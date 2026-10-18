import Link from "next/link";

export default function DiscordSetupPage() {
  return (
    <main className="workspace-page">
      <header className="workspace-nav">
        <Link className="brand" href="/" aria-label="SigmaAgent home">
          <span className="sigma-mark" aria-hidden="true">Σ</span>
          <span className="brand-name">SigmaAgent<span>Company Brain</span></span>
        </Link>
      </header>
      <section className="workspace-shell" aria-labelledby="discord-setup-title">
        <p className="eyebrow">CONNECT YOUR TEAM</p>
        <h1 id="discord-setup-title">Add SigmaAgent to Discord</h1>
        <p className="workspace-intro">
          The bot uses the same investigation service as this workspace. To create the server install link, set
          <code> DISCORD_APPLICATION_ID </code> in <code>website/.env</code>, or set a real invite URL with
          <code> DISCORD_INVITE_URL</code>, then restart the website.
        </p>
        <div className="workspace-result">
          <div className="workspace-empty">
            <span className="workspace-empty-mark" aria-hidden="true">Σ</span>
            <p>After connecting, mention <strong>@SigmaAgent</strong> in an allowed server channel to investigate blockers, ownership, and supporting context.</p>
          </div>
        </div>
        <p className="workspace-footnote">The bot needs permission to view channels, read message history, and send replies. Message Content Intent must also be enabled in the Discord Developer Portal.</p>
        <p><Link className="button-secondary" href="/workspace">Return to the investigation workspace →</Link></p>
      </section>
    </main>
  );
}
