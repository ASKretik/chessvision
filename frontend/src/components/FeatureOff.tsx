/**
 * A tab whose feature flag is false renders its page frame with this, never a
 * blank panel and never a dead button. The reason comes from /config so the
 * UI states the backend's own explanation rather than guessing.
 */
export function FeatureOff({ name, reason }: { name: string; reason?: string }) {
  return (
    <main className="page">
      <div className="panel" style={{ padding: 'var(--sp-5)', maxWidth: '60ch' }}>
        <h1 style={{ marginTop: 0 }}>{name} is not available</h1>
        <p style={{ color: 'var(--text-dim)' }}>
          {reason ?? 'The server reports this feature is switched off in this deployment.'}
        </p>
      </div>
    </main>
  );
}
