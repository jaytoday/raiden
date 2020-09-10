from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass

import gevent.lock
import structlog

from raiden.storage.serialization import DictSerializer
from raiden.storage.sqlite import (
    HIGH_STATECHANGE_ULID,
    LOW_STATECHANGE_ULID,
    EventID,
    Range,
    SerializedSQLiteStorage,
    StateChangeID,
    write_events,
    write_state_change,
)
from raiden.transfer.architecture import Event, State, StateChange, StateManager
from raiden.utils.formatting import to_checksum_address
from raiden.utils.logging import redact_secret
from raiden.utils.typing import (
    Address,
    Callable,
    Generator,
    Generic,
    List,
    Optional,
    RaidenDBVersion,
    Tuple,
    TypeVar,
)

log = structlog.get_logger(__name__)


ST = TypeVar("ST", bound=State)
ST2 = TypeVar("ST2", bound=State)


def restore_or_init_snapshot(
    storage: SerializedSQLiteStorage, node_address: Address, initial_state: State
) -> Tuple[State, StateChangeID, int]:
    """ Restore the latest snapshot.
    Returns the ULID of the state change that is not applied and the
    accumulated number of state_changes applied to this snapshot so far.  If
    there is no snapshot the state will be primed with `initial_state`.
    """

    state_change_identifier = HIGH_STATECHANGE_ULID

    snapshot = storage.get_snapshot_before_state_change(
        state_change_identifier=state_change_identifier
    )

    if snapshot is not None:
        log.debug(
            "Snapshot found",
            from_state_change_id=snapshot.state_change_identifier,
            to_state_change_id=state_change_identifier,
            node=to_checksum_address(node_address),
        )
        return snapshot.data, snapshot.state_change_identifier, snapshot.state_change_qty
    else:
        log.debug(
            "No snapshot found, initializing the node state",
            to_state_change_id=state_change_identifier,
            node=to_checksum_address(node_address),
        )
        # The initial state must be saved to preserve the state of the PRNG
        storage.write_first_state_snapshot(initial_state)
        return initial_state, LOW_STATECHANGE_ULID, 0


def replay_unapplied_state_changes(
    transition_function: Callable,
    storage: SerializedSQLiteStorage,
    unapplied_state_changes_range: Range,
    node_address: Address,
    state_snapshot: State,
) -> Tuple[int, State]:
    """Applies the state changes in the range `unapplied_state_changes_range`
    into the `snapshot_state`.
    """

    unapplied_state_changes = storage.get_statechanges_by_range(unapplied_state_changes_range)

    log.debug(
        "Replaying state changes",
        replayed_state_changes=[
            redact_secret(DictSerializer.serialize(state_change))
            for state_change in unapplied_state_changes
        ],
        node=to_checksum_address(node_address),
    )

    state_manager = StateManager(transition_function, state_snapshot, unapplied_state_changes)

    return len(unapplied_state_changes), state_manager.current_state


def restore_state(
    transition_function: Callable,
    storage: SerializedSQLiteStorage,
    state_change_identifier: StateChangeID,
    node_address: Address,
) -> Optional[State]:
    snapshot = storage.get_snapshot_before_state_change(
        state_change_identifier=state_change_identifier
    )

    if snapshot is None:
        return None

    log.debug(
        "Snapshot found",
        from_state_change_id=snapshot.state_change_identifier,
        to_state_change_id=state_change_identifier,
        node=to_checksum_address(node_address),
    )
    unapplied_state_changes = storage.get_statechanges_by_range(
        Range(snapshot.state_change_identifier, state_change_identifier)
    )
    log.debug(
        "Replaying state changes",
        replayed_state_changes=[
            redact_secret(DictSerializer.serialize(state_change))
            for state_change in unapplied_state_changes
        ],
        node=to_checksum_address(node_address),
    )

    state_manager = StateManager(transition_function, snapshot.data, unapplied_state_changes)

    return state_manager.current_state


@dataclass(frozen=True)
class SavedState(Generic[ST]):
    """Saves the state and the id of the state change that produced it.

    This datastructure keeps the state and the state_change_id synchronized.
    Having these values available is useful for debugging.
    """

    state_change_id: StateChangeID
    state: ST


class AtomicStateChangeDispatcher(ABC, Generic[ST]):
    @abstractmethod
    def dispatch(self, state_change: StateChange) -> List[Event]:
        pass

    @abstractmethod
    def latest_state(self) -> ST:
        pass


class WriteAheadLog(Generic[ST]):
    saved_state: SavedState[ST]

    def __init__(self, state_manager: StateManager[ST], storage: SerializedSQLiteStorage) -> None:
        self._state_manager = state_manager
        self.storage = storage

        # The state changes must be applied in the same order as they are saved
        # to the WAL. Because writing to the database context switches, and the
        # scheduling is undetermined, a lock is necessary to protect the
        # execution order.
        self._lock = gevent.lock.Semaphore()

    @contextmanager
    def process_state_change_atomically(
        self,
    ) -> Generator[AtomicStateChangeDispatcher, None, None]:
        class _AtomicStateChangeDispatcher(AtomicStateChangeDispatcher, Generic[ST2]):
            def __init__(
                self, state_manager: StateManager[ST2], storage: SerializedSQLiteStorage
            ) -> None:
                self._state_manager = state_manager
                self.storage = storage

                self.last_state_change_id: Optional[StateChangeID] = None

            def dispatch(self, state_change: StateChange) -> List[Event]:
                _, events = self._state_manager.dispatch(state_change)
                state_change_id = self.write_state_change_and_events(state_change, events)

                self.last_state_change_id = state_change_id

                return events

            def latest_state(self) -> ST:
                return self._state_manager.current_state

            def write_state_change_and_events(
                self, state_change: StateChange, events: List[Event]
            ) -> StateChangeID:
                cursor = self.storage.database.conn.cursor()

                state_change_id = write_state_change(
                    ulid_factory=self.storage.database._ulid_factory(StateChangeID),
                    cursor=cursor,
                    state_change=self.storage.serializer.serialize(state_change),
                )

                event_data = list()
                for event in events:
                    event_data.append((state_change_id, self.storage.serializer.serialize(event)))

                write_events(
                    ulid_factory=self.storage.database._ulid_factory(EventID),
                    cursor=cursor,
                    events=event_data,
                )

                return state_change_id

        with self._lock:
            copied_state_manager = self._state_manager.copy()

            with self.storage.database.transaction():
                dispatcher = _AtomicStateChangeDispatcher(
                    state_manager=copied_state_manager, storage=self.storage,
                )
                yield dispatcher

            self._state_manager = copied_state_manager

            # When no state change was applied, do not update saved state
            if dispatcher.last_state_change_id is not None:
                # The update must be done with a single operation, to make sure
                # that readers will have a consistent view of it.

                assert self._state_manager.current_state is not None, "state is None"
                self.saved_state = SavedState(
                    dispatcher.last_state_change_id, self._state_manager.current_state
                )

    def snapshot(self, statechange_qty: int) -> None:
        """ Snapshot the application state.

        Snapshots are used to restore the application state, either after a
        restart or a crash.
        """
        with self._lock:
            current_state = self._state_manager.current_state
            state_change_id = self.saved_state.state_change_id

            # otherwise no state change was dispatched
            if state_change_id and current_state is not None:
                self.storage.write_state_snapshot(current_state, state_change_id, statechange_qty)

    def get_current_state(self) -> ST:
        """Returns the current node state."""
        return self._state_manager.current_state

    @property
    def version(self) -> RaidenDBVersion:
        return self.storage.get_version()
