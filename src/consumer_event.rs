//! Process-local observations of PostgreSQL consumer initialization.
//!
//! These payload-free events are delivered through the owning worker's native
//! event listener. They confer no database or job authority. Consumers must bind
//! them to the actual worker attempt and revoke progress when that attempt stops.

use apalis_core::worker::{context::WorkerContext, event::Event};

/// The transport selected by an actual PostgreSQL backend enrollment.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConsumerTransport {
    /// Repeated queue fetches without a persistent notification listener.
    Polling,
    /// Queue fetches plus a persistent PostgreSQL LISTEN connection.
    Notify,
}

/// Native consumer observations, including successful empty queue fetches.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConsumerEvent {
    /// The backend was enrolled before its first I/O, not yet initialized.
    Enrolled(ConsumerTransport),
    /// A queue fetch and compact row conversion completed successfully.
    FetchReady,
    /// The notification connection completed its actual LISTEN statement.
    NotifyReady,
}

impl ConsumerEvent {
    pub(crate) fn emit(self, worker: &WorkerContext) {
        worker.clone().emit(&Event::custom(self));
    }
}
