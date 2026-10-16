const discordInvite = process.env.NEXT_PUBLIC_DISCORD_INVITE_URL || "#how-it-works";

const features = [
  {
    number: "01",
    title: "Find the blocker",
    description: "Ask a question in Discord and get a concise investigation instead of searching through multiple tools.",
  },
  {
    number: "02",
    title: "Follow dependencies",
    description: "Understand which tasks and projects may be affected by a delay.",
  },
  {
    number: "03",
    title: "See the evidence",
    description: "Bring relevant task details, decisions, and conversation context together when the connected sources provide them.",
  },
];

export default function Home() {
  return (
    <main>
      <nav className="nav">
        <a className="brand" href="#" aria-label="SigmaAgent home">
          <span className="brand-mark">Σ</span>
          <span>SigmaAgent</span>
        </a>
        <a className="nav-link" href="#how-it-works">How it works</a>
        <a className="nav-cta" href={discordInvite}>Try on Discord <span aria-hidden="true">↗</span></a>
      </nav>

      <section className="hero">
        <div className="eyebrow"><span className="status-dot" /> PROJECT INTELLIGENCE AGENT</div>
        <h1>Know what’s blocking the work.</h1>
        <p className="hero-copy">
          SigmaAgent connects project tasks, dependencies, and team context to help
          your team understand delays and decide what to do next — right from Discord.
        </p>
        <div className="hero-actions">
          <a className="primary-button" href={discordInvite}>Meet SigmaAgent on Discord <span aria-hidden="true">↗</span></a>
          <a className="secondary-button" href="#how-it-works">See how it works</a>
        </div>

        <div className="demo" aria-label="Example Discord investigation">
          <div className="demo-top">
            <div className="window-dots"><i /><i /><i /></div>
            <span>EXAMPLE INVESTIGATION</span>
            <span className="demo-tag">READ-ONLY</span>
          </div>
          <div className="chat-line user-line">
            <div className="avatar user-avatar">S</div>
            <div>
              <div className="chat-meta">seraj <span>Today at 10:42 AM</span></div>
              <p><b>@SigmaAgent</b> Why is checkout delayed?</p>
            </div>
          </div>
          <div className="chat-line bot-line">
            <div className="avatar bot-avatar">Σ</div>
            <div className="answer">
              <div className="chat-meta">SigmaAgent <span className="bot-label">APP</span></div>
              <p className="answer-title">Investigation summary</p>
              <div className="answer-row"><span className="answer-key">Likely blocker</span><span>Waiting on the payment API dependency</span></div>
              <div className="answer-row"><span className="answer-key">Owner</span><span>Identified from linked task data, when available</span></div>
              <div className="answer-row"><span className="answer-key">Potential impact</span><span>Checkout QA may need to move</span></div>
              <p className="demo-note">Illustrative example — actual findings depend on connected project data.</p>
            </div>
          </div>
        </div>
        <p className="hero-footnote">One question. Connected context. A clearer next step.</p>
      </section>

      <section className="features" id="how-it-works">
        <div className="section-heading">
          <p className="eyebrow">LESS STATUS-CHASING, MORE CLARITY</p>
          <h2>From “What happened?” to “What’s next?”</h2>
          <p>Get the context behind project progress without manually chasing every thread.</p>
        </div>
        <div className="feature-grid">
          {features.map((feature) => (
            <article className="feature" key={feature.number}>
              <span className="feature-number">{feature.number}</span>
              <h3>{feature.title}</h3>
              <p>{feature.description}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="closing">
        <p className="eyebrow">BUILT FOR TEAMS THAT SHIP</p>
        <h2>Make project context easier to find.</h2>
        <p>Ask in Discord. Review the evidence. Choose the next action as a team.</p>
        <a className="primary-button" href={discordInvite}>Get started <span aria-hidden="true">↗</span></a>
      </section>

      <footer className="footer">
        <a className="brand" href="#"><span className="brand-mark">Σ</span><span>SigmaAgent</span></a>
        <span>Project intelligence for clearer decisions.</span>
      </footer>
    </main>
  );
}
