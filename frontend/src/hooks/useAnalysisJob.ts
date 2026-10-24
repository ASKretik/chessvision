import { useEffect, useRef, useState } from 'react';
import { api, jobEventsUrl } from '../api/client';
import { AppError, type JobStage, type Report } from '../api/types';

/* Silence longer than this means the connection dropped. The backend sends
 * `: keep-alive` comments every 15s, so 45s is three missed beats. */
const KEEPALIVE_TIMEOUT_MS = 45_000;
const POLL_INTERVAL_MS = 3_000;

export interface AnalysisJobState {
  /** Set by the `analysis` event, LONG before the job finishes. Render it the
   *  moment it exists — this is the single biggest win for perceived speed. */
  report: Report | null;
  stage: JobStage | null;
  progress: number;
  /** True once `result` lands. Commentary is complete only now. */
  done: boolean;
  error: AppError | null;
  /** The stream dropped and we fell back to polling. Worth a quiet hint. */
  polling: boolean;
}

const initial: AnalysisJobState = {
  report: null,
  stage: null,
  progress: 0,
  done: false,
  error: null,
  polling: false,
};

/**
 * Owns the EventSource for one analysis job.
 *
 * The stream replays every event from the start, so mounting this on a fresh
 * page load after a refresh loses nothing — which is why the job id is a real
 * route (`/analyse/:jobId`) and not component state.
 */
export function useAnalysisJob(jobId: string | undefined): AnalysisJobState {
  const [state, setState] = useState<AnalysisJobState>(initial);

  // Held in a ref so the keepalive watchdog can tear down the stream without
  // being a dependency of the effect that created it.
  const sourceRef = useRef<EventSource | null>(null);
  const pollRef = useRef<number | null>(null);
  const watchdogRef = useRef<number | null>(null);

  useEffect(() => {
    if (!jobId) {
      setState(initial);
      return;
    }

    setState(initial);
    let cancelled = false;

    const stopPolling = () => {
      if (pollRef.current != null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };

    const startPolling = () => {
      if (pollRef.current != null) return;
      setState((s) => ({ ...s, polling: true }));
      pollRef.current = window.setInterval(async () => {
        if (cancelled) return;
        try {
          // GET /jobs/{id} calls the report `result`, and a failed job is a
          // 200 with state 'failed' -- not an HTTP error.
          const snap = await api.job(jobId);
          setState((s) => ({
            ...s,
            stage: snap.stage ?? s.stage,
            progress: snap.progress ?? s.progress,
            report: snap.result ?? s.report,
            done: snap.state === 'done',
            error:
              snap.state === 'failed'
                ? new AppError('unknown', 0, snap.error ?? 'Analysis failed')
                : s.error,
          }));
          if (snap.state === 'done' || snap.state === 'failed') stopPolling();
        } catch (err) {
          if (err instanceof AppError && err.kind === 'expired') {
            setState((s) => ({ ...s, error: err }));
            stopPolling();
          }
          // Any other failure: keep polling. The server may just be waking.
        }
      }, POLL_INTERVAL_MS);
    };

    const armWatchdog = () => {
      if (watchdogRef.current != null) window.clearTimeout(watchdogRef.current);
      watchdogRef.current = window.setTimeout(() => {
        sourceRef.current?.close();
        sourceRef.current = null;
        startPolling();
      }, KEEPALIVE_TIMEOUT_MS);
    };

    const es = new EventSource(jobEventsUrl(jobId));
    sourceRef.current = es;
    armWatchdog();

    const on = (name: string, fn: (data: unknown) => void) => {
      es.addEventListener(name, (ev) => {
        armWatchdog();
        if (cancelled) return;
        try {
          fn(JSON.parse((ev as MessageEvent<string>).data));
        } catch {
          /* a malformed frame is not worth failing the whole job over */
        }
      });
    };

    on('state', (d) => {
      const data = d as { state?: string; stage?: JobStage };
      setState((s) => ({
        ...s,
        stage: data.stage ?? s.stage,
        done: data.state === 'done' ? true : s.done,
      }));
    });

    on('progress', (d) => {
      const data = d as { stage?: JobStage; progress?: number };
      setState((s) => ({
        ...s,
        stage: data.stage ?? s.stage,
        progress: data.progress ?? s.progress,
      }));
    });

    // The whole Part A report, before commentary. Render everything now.
    // The frame carries the report at the TOP level -- partE publishes
    // job.result directly, not wrapped in {report}.
    on('analysis', (d) => {
      const report = d as Report;
      if (report?.kind) {
        setState((s) => ({ ...s, report, progress: Math.max(s.progress, 0.6) }));
      }
    });

    // Final payload, commentary included. Replaces the report and ends.
    on('result', (d) => {
      const report = d as Report;
      setState((s) => ({
        ...s,
        report: report?.kind ? report : s.report,
        progress: 1,
        done: true,
        stage: 'done',
      }));
      es.close();
      sourceRef.current = null;
      if (watchdogRef.current != null) window.clearTimeout(watchdogRef.current);
    });

    // partE sends {error}, not {detail}: a failed job is a normal event on a
    // healthy stream, so this is the only place the failure surfaces.
    on('error', (d) => {
      const data = d as { error?: string };
      setState((s) => ({
        ...s,
        error: new AppError('unknown', 0, data.error ?? 'Analysis failed'),
      }));
    });

    // Transport-level failure, as opposed to our own `error` event. Browsers
    // auto-reconnect on their own; the watchdog handles the case where they
    // cannot, so we only flip to polling if the socket is really closed.
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        sourceRef.current = null;
        startPolling();
      }
    };

    return () => {
      cancelled = true;
      es.close();
      sourceRef.current = null;
      stopPolling();
      if (watchdogRef.current != null) window.clearTimeout(watchdogRef.current);
    };
  }, [jobId]);

  return state;
}
