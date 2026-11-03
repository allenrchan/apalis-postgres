//! Native PostgreSQL lifecycle proof; the owner/CI supplies DATABASE_URL.
//! No provider credentials, customer payloads or test-owned Docker daemon.
use std::time::Duration;

use apalis::prelude::*;
use apalis_core::monitor::MonitorError;
use apalis_postgres::{Config, ConsumerEvent, ConsumerTransport, PgPool, PostgresStorage};
use sqlx::postgres::PgPoolOptions;
use tokio::{
    sync::{mpsc, oneshot},
    task::JoinHandle,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Signal {
    Start,
    Registration,
    Native(ConsumerEvent),
    Stop,
}

struct Observation {
    name: String,
    context: WorkerContext,
    signal: Signal,
}

struct RunningMonitor {
    events: mpsc::UnboundedReceiver<Observation>,
    observed: Vec<Observation>,
    stop: Option<oneshot::Sender<()>>,
    runner: JoinHandle<Result<(), MonitorError>>,
}

impl RunningMonitor {
    fn start(monitor: Monitor) -> Self {
        let (seen, events) = mpsc::unbounded_channel();
        let monitor = monitor.on_event(move |context, event| {
            let signal = match event {
                Event::Start => Signal::Start,
                Event::Idle => Signal::Registration,
                Event::Stop => Signal::Stop,
                Event::Custom(value) => match value.downcast_ref::<ConsumerEvent>() {
                    Some(event) => Signal::Native(*event),
                    None => return,
                },
                _ => return,
            };
            let _ = seen.send(Observation {
                name: context.name().clone(),
                context: context.clone(),
                signal,
            });
        });
        let (stop, stopping) = oneshot::channel();
        let runner = tokio::spawn(monitor.run_with_signal(async move {
            let _ = stopping.await;
            Ok(())
        }));
        Self {
            events,
            observed: Vec::new(),
            stop: Some(stop),
            runner,
        }
    }

    async fn wait_for(&mut self, name: &str, signal: Signal) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !self
                .observed
                .iter()
                .any(|item| item.name == name && item.signal == signal)
            {
                let item = self
                    .events
                    .recv()
                    .await
                    .expect("monitor must remain observable");
                self.observed.push(item);
            }
        })
        .await
        .expect("bounded actual native event");
    }

    async fn finish(&mut self, success: bool) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        let outcome = tokio::time::timeout(Duration::from_secs(5), &mut self.runner)
            .await
            .expect("bounded native monitor shutdown")
            .expect("native runner task");
        assert_eq!(
            outcome.is_ok(),
            success,
            "native error propagation is preserved"
        );
    }

    fn native_for(&self, name: &str) -> Vec<ConsumerEvent> {
        self.observed
            .iter()
            .filter_map(|item| match item.signal {
                Signal::Native(event) if item.name == name => Some(event),
                _ => None,
            })
            .collect()
    }
}

impl Drop for RunningMonitor {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        self.runner.abort();
    }
}

fn names() -> (String, String) {
    (
        format!("native-ready-{}", ulid::Ulid::new()),
        format!("empty-ready-{}", ulid::Ulid::new()),
    )
}

async fn setup(pool: &PgPool) {
    PostgresStorage::setup(pool)
        .await
        .expect("prepare actual isolated queue");
}

async fn assert_empty_and_registered(pool: &PgPool, worker: &str, queue: &str) {
    let registered: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM apalis.workers WHERE id=$1 AND worker_type=$2)",
    )
    .bind(worker)
    .bind(queue)
    .fetch_one(pool)
    .await
    .expect("actual worker registration");
    let jobs: i64 = sqlx::query_scalar("SELECT count(*) FROM apalis.jobs WHERE job_type=$1")
        .bind(queue)
        .fetch_one(pool)
        .await
        .expect("actual empty queue");
    assert!(registered);
    assert_eq!(jobs, 0, "initialization must not manufacture customer work");
}

async fn consumer_pool(
    pool: &PgPool,
    maximum: u32,
    timeout: Duration,
    application: &str,
) -> PgPool {
    PgPoolOptions::new()
        .max_connections(maximum)
        .acquire_timeout(timeout)
        .connect_with(
            (*pool.connect_options())
                .clone()
                .application_name(application),
        )
        .await
        .expect("isolated consumer pool")
}

#[sqlx::test(migrations = false)]
async fn empty_postgres_consumer_reports_native_progress_without_a_customer_job(pool: PgPool) {
    setup(&pool).await;
    let (worker, queue) = names();
    let storage = PostgresStorage::<u32>::new_with_config(&pool, &Config::new(&queue));
    let id = worker.clone();
    let mut running = RunningMonitor::start(Monitor::new().register(move |_| {
        WorkerBuilder::new(&id)
            .backend(storage.clone())
            .build(|_: u32| async { Ok::<(), BoxDynError>(()) })
    }));
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::FetchReady))
        .await;
    assert_eq!(
        running.native_for(&worker),
        vec![
            ConsumerEvent::Enrolled(ConsumerTransport::Polling),
            ConsumerEvent::FetchReady
        ]
    );
    let enrollment = running
        .observed
        .iter()
        .position(|item| {
            item.signal == Signal::Native(ConsumerEvent::Enrolled(ConsumerTransport::Polling))
        })
        .expect("real enrollment");
    let start = running
        .observed
        .iter()
        .position(|item| item.signal == Signal::Start)
        .expect("native start");
    assert!(
        enrollment < start,
        "actual backend enrollment is observed before Start and I/O"
    );
    assert_empty_and_registered(&pool, &worker, &queue).await;
    assert!(!running.runner.is_finished());
    running.finish(true).await;
}

#[sqlx::test(migrations = false)]
async fn empty_notify_consumer_reports_fetch_and_actual_listen_completion(pool: PgPool) {
    setup(&pool).await;
    let (worker, queue) = names();
    let consumers = consumer_pool(&pool, 3, Duration::from_secs(2), &worker).await;
    let storage = PostgresStorage::<u32>::new_with_notify(&consumers, &Config::new(&queue));
    let id = worker.clone();
    let mut running = RunningMonitor::start(Monitor::new().register(move |_| {
        WorkerBuilder::new(&id)
            .backend(storage.clone())
            .build(|_: u32| async { Ok::<(), BoxDynError>(()) })
    }));
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::NotifyReady))
        .await;
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::FetchReady))
        .await;
    let native = running.native_for(&worker);
    assert_eq!(
        native.first(),
        Some(&ConsumerEvent::Enrolled(ConsumerTransport::Notify))
    );
    let listeners: i64 = sqlx::query_scalar("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name=$1 AND query LIKE 'LISTEN %'")
        .bind(&worker).fetch_one(&pool).await.expect("actual persistent LISTEN session");
    assert_eq!(listeners, 1);
    assert_empty_and_registered(&pool, &worker, &queue).await;
    running.finish(true).await;
    consumers.close().await;
}

#[sqlx::test(migrations = false)]
async fn saturated_pool_enrolls_but_cannot_report_fetch_or_listen_before_release(pool: PgPool) {
    setup(&pool).await;
    let (worker, queue) = names();
    let consumers = consumer_pool(&pool, 2, Duration::from_secs(3), &worker).await;
    let first = consumers.acquire().await.expect("first held pool slot");
    let second = consumers.acquire().await.expect("second held pool slot");
    let storage = PostgresStorage::<u32>::new_with_notify(&consumers, &Config::new(&queue));
    let id = worker.clone();
    let mut running = RunningMonitor::start(Monitor::new().register(move |_| {
        WorkerBuilder::new(&id)
            .backend(storage.clone())
            .build(|_: u32| async { Ok::<(), BoxDynError>(()) })
    }));
    running
        .wait_for(
            &worker,
            Signal::Native(ConsumerEvent::Enrolled(ConsumerTransport::Notify)),
        )
        .await;
    let progress = tokio::time::timeout(Duration::from_millis(200), async {
        while let Some(item) = running.events.recv().await {
            let initialized = matches!(
                item.signal,
                Signal::Native(ConsumerEvent::FetchReady | ConsumerEvent::NotifyReady)
            );
            running.observed.push(item);
            if initialized {
                return;
            }
        }
    })
    .await;
    assert!(
        progress.is_err(),
        "a registered/enrolled consumer is not initialized through another pool"
    );
    assert!(!running.runner.is_finished());
    drop(first);
    drop(second);
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::FetchReady))
        .await;
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::NotifyReady))
        .await;
    running.finish(true).await;
    consumers.close().await;
}

#[sqlx::test(migrations = false)]
async fn notify_listener_owns_a_real_pool_slot_and_fetch_failure_stops_attempt(pool: PgPool) {
    setup(&pool).await;
    let (worker, queue) = names();
    let consumers = consumer_pool(&pool, 1, Duration::from_millis(300), &worker).await;
    let storage = PostgresStorage::<u32>::new_with_notify(&consumers, &Config::new(&queue));
    let id = worker.clone();
    let mut running = RunningMonitor::start(Monitor::new().register(move |_| {
        WorkerBuilder::new(&id)
            .backend(storage.clone())
            .build(|_: u32| async { Ok::<(), BoxDynError>(()) })
    }));
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::NotifyReady))
        .await;
    assert_eq!(consumers.size(), 1);
    assert!(
        consumers.try_acquire().is_none(),
        "LISTEN holds the same pool's only slot"
    );
    running.wait_for(&worker, Signal::Stop).await;
    assert!(
        running
            .observed
            .iter()
            .filter(|item| item.name == worker && item.signal == Signal::Stop)
            .all(|item| !item.context.is_running())
    );
    running.finish(false).await;
    consumers.close().await;
}

#[sqlx::test(migrations = false)]
async fn unavailable_pool_never_initializes_and_native_restart_has_a_new_attempt_identity(
    pool: PgPool,
) {
    setup(&pool).await;
    let (base, queue) = names();
    let broken = consumer_pool(&pool, 2, Duration::from_secs(1), &base).await;
    broken.close().await;
    let healthy = pool.clone();
    let prefix = base.clone();
    let mut running = RunningMonitor::start(
        Monitor::new()
            .should_restart(|_, _, attempt| attempt == 0)
            .register(move |attempt| {
                let pool = if attempt == 0 { &broken } else { &healthy };
                let storage = PostgresStorage::<u32>::new_with_config(pool, &Config::new(&queue));
                WorkerBuilder::new(format!("{prefix}-{attempt}"))
                    .backend(storage)
                    .build(|_: u32| async { Ok::<(), BoxDynError>(()) })
            }),
    );
    let old = format!("{base}-0");
    let new = format!("{base}-1");
    running
        .wait_for(&new, Signal::Native(ConsumerEvent::FetchReady))
        .await;
    assert_eq!(
        running.native_for(&old),
        vec![ConsumerEvent::Enrolled(ConsumerTransport::Polling)]
    );
    assert!(
        running.observed.iter().any(|item| item.name == old
            && item.signal == Signal::Stop
            && !item.context.is_running())
    );
    assert_eq!(
        running.native_for(&new),
        vec![
            ConsumerEvent::Enrolled(ConsumerTransport::Polling),
            ConsumerEvent::FetchReady
        ]
    );
    running.finish(true).await;
}

#[sqlx::test(migrations = false)]
async fn initialized_polling_attempt_stops_when_its_own_database_pool_closes(pool: PgPool) {
    setup(&pool).await;
    let (worker, queue) = names();
    let consumers = consumer_pool(&pool, 2, Duration::from_secs(1), &worker).await;
    let storage = PostgresStorage::<u32>::new_with_config(&consumers, &Config::new(&queue));
    let id = worker.clone();
    let mut running = RunningMonitor::start(Monitor::new().register(move |_| {
        WorkerBuilder::new(&id)
            .backend(storage.clone())
            .build(|_: u32| async { Ok::<(), BoxDynError>(()) })
    }));
    running
        .wait_for(&worker, Signal::Native(ConsumerEvent::FetchReady))
        .await;
    consumers.close().await;
    running.wait_for(&worker, Signal::Stop).await;
    assert!(
        running
            .observed
            .iter()
            .filter(|item| item.name == worker && item.signal == Signal::Stop)
            .all(|item| !item.context.is_running())
    );
    let successes = running
        .native_for(&worker)
        .into_iter()
        .filter(|event| *event == ConsumerEvent::FetchReady)
        .count();
    assert_eq!(
        successes, 1,
        "a failed fetch cannot emit another successful progress observation"
    );
    running.finish(false).await;
}
