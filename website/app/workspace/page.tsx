"use client";

import Link from "next/link";
import { type ReactNode, useState } from "react";
import ThemeToggle from "@/app/components/ThemeToggle";

type Source = { title: string; url: string; type: string };
type Investigation = { answer: string; sources: Source[] };

const suggestions = [
  "What is blocking the release?",
  "Why is T5 delayed?",
  "Who owns the next action?",
  "What decisions affect this work?",
];

function AnswerText({ text }: { text: string }) {
  function renderSegments(content: string): ReactNode {
    return content.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).map((part, partIndex) => {
      if (part.startsWith("**") && part.endsWith("**")) return <strong key={partIndex}>{part.slice(2, -2)}</strong>;
      if (part.startsWith("*") && part.endsWith("*")) return <em key={partIndex}>{part.slice(1, -1)}</em>;
      return <span key={partIndex}>{part}</span>;
    });
  }

  return (
    <div className="workspace-answer-text">
      {text.split(/\n+/).map((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) return null;
        const item = trimmed.match(/^[-*]\s+(.+)/);
        const content = item ? item[1] : trimmed;
        return item ? (
          <div className="workspace-answer-list-item" key={`${index}-${trimmed}`}>
            <span aria-hidden="true">•</span>
            <p>{renderSegments(content)}</p>
          </div>
        ) : (
          <p key={`${index}-${trimmed}`}>{renderSegments(content)}</p>
        );
      })}
    </div>
  );
}

export default function WorkspacePage() {
  const [question, setQuestion] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [investigation, setInvestigation] = useState<Investigation | null>(null);

  async function ask(value: string) {
    value = value.trim();
    if (value.length < 3 || pending) return;

    setQuestion(value);
    setPending(true);
    setError("");
    setInvestigation(null);
    try {
      const response = await fetch("/api/investigate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        },
        body: JSON.stringify({ question: value }),
      });
      const payload: unknown = await response.json().catch(() => null);
      const data = payload && typeof payload === "object" ? payload as { answer?: unknown; error?: unknown; sources?: unknown } : null;
      if (!response.ok) {
        const message = typeof data?.error === "string" ? data.error : "SigmaAgent could not answer right now.";
        throw new Error(message);
      }
      if (!data || typeof data.answer !== "string") {
        throw new Error("SigmaAgent returned an unreadable response. Please try again.");
      }
      setInvestigation({
        answer: data.answer,
        sources: Array.isArray(data.sources) ? data.sources as Source[] : [],
      });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "SigmaAgent could not answer right now.");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="workspace-page">
      <header className="workspace-nav">
        <Link className="brand" href="/" aria-label="SigmaAgent home">
          <span className="sigma-mark" aria-hidden="true">Σ</span>
          <span className="brand-name">SigmaAgent<span>Company Brain</span></span>
        </Link>
        <nav aria-label="Workspace navigation">
          {/* These native anchors target sections in the public HTML landing page. */}
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
          <a href="/#capabilities">How it helps</a>
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
          <a href="/#workspace">Sample answers</a>
          <Link href="/discord">Use in Discord</Link>
        </nav>
        <span className="workspace-mode"><i /> INVESTIGATION WORKSPACE</span>
        <ThemeToggle />
      </header>

      <section className="workspace-shell" aria-labelledby="workspace-title">
        <p className="eyebrow">YOUR TEAM&apos;S CONNECTED CONTEXT</p>
        <h1 id="workspace-title">What would you like to understand?</h1>
        <p className="workspace-intro">Ask about a blocker, task, teammate, or decision. SigmaAgent will look for an answer in the configured company workspace.</p>

        <label className="workspace-access-label" htmlFor="workspace-access">Workspace access key <span>Optional in local development</span></label>
        <input
          className="workspace-access-input"
          id="workspace-access"
          type="password"
          autoComplete="off"
          value={accessToken}
          onChange={(event) => setAccessToken(event.target.value)}
          placeholder="Enter your team access key if required"
        />

        <form className="workspace-question-form" onSubmit={(event) => { event.preventDefault(); void ask(question); }}>
          <label className="sr-only" htmlFor="workspace-question">Ask SigmaAgent a question</label>
          <textarea
            id="workspace-question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="For example: What is blocking the release?"
            maxLength={500}
            rows={3}
            required
            minLength={3}
          />
          <div className="workspace-form-footer">
            <span>Read-only · Answers may be limited by available team context</span>
            <button className="primary-button" type="submit" disabled={pending || question.trim().length < 3}>
              {pending ? "Looking…" : "Ask SigmaAgent"}<span aria-hidden="true">↗</span>
            </button>
          </div>
        </form>

        <div className="workspace-suggestions" aria-label="Suggested questions">
          <span className="micro-label">TRY ASKING</span>
          {suggestions.map((suggestion) => (
            <button key={suggestion} type="button" onClick={() => void ask(suggestion)} disabled={pending}>
              {suggestion}<span aria-hidden="true">↗</span>
            </button>
          ))}
        </div>

        <section className="workspace-result" aria-live="polite" aria-busy={pending}>
          {pending && <div className="workspace-state"><span className="workspace-spinner" aria-hidden="true" /><div><strong>Looking through connected work</strong><p>Checking for relevant tasks and context…</p></div></div>}
          {error && <div className="workspace-error" role="alert"><strong>Couldn&apos;t complete that investigation</strong><p>{error}</p><button type="button" onClick={() => void ask(question)}>Try again</button></div>}
          {investigation && <article className="workspace-investigation">
            <div className="workspace-result-heading"><span className="evidence-status"><i /> INVESTIGATION</span><span className="micro-label">READ-ONLY ANSWER</span></div>
            <h2>{question}</h2>
            <AnswerText text={investigation.answer} />
            {investigation.sources.length > 0 ? <div className="workspace-sources"><span className="micro-label">SUPPORTING SOURCES</span>{investigation.sources.map((source) => <a key={`${source.url}-${source.title}`} href={source.url} target="_blank" rel="noreferrer"><span>{source.type === "discord" ? "Conversation" : source.type === "github" ? "GitHub" : "Source"}</span><strong>{source.title}</strong><span aria-hidden="true">↗</span></a>)}</div> : <p className="workspace-no-sources">No linked source was returned for this answer. Treat it as unverified until you can confirm the context.</p>}
          </article>}
          {!pending && !error && !investigation && <div className="workspace-empty"><span className="workspace-empty-mark" aria-hidden="true">⌘</span><p>Your investigation will appear here with the answer and any linked sources.</p></div>}
        </section>
        <p className="workspace-footnote">This workspace is read-only. Use Discord for approved task updates.</p>
      </section>
    </main>
  );
}
