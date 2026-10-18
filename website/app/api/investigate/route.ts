import { NextRequest, NextResponse } from "next/server";
import { timingSafeEqual } from "node:crypto";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_QUESTION_LENGTH = 500;
const MUTATION_INTENT =
  /^\s*(?:please\s+)?(?:create|add|assign|unassign|block|unblock|complete|finish|mark|set|update|rename|delete|remove|archive|restore|join|leave|link)\b/i;

type BackendResponse = {
  answer?: unknown;
  sources?: unknown;
};

function isSafeSource(source: unknown): source is { title: string; url: string; type: string } {
  if (!source || typeof source !== "object") return false;
  const candidate = source as { title?: unknown; url?: unknown; type?: unknown };
  if (typeof candidate.title !== "string" || typeof candidate.url !== "string" || typeof candidate.type !== "string") {
    return false;
  }
  try {
    return ["http:", "https:"].includes(new URL(candidate.url).protocol);
  } catch {
    return false;
  }
}

export async function POST(request: NextRequest) {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (origin && host && new URL(origin).host !== host) {
    return NextResponse.json({ error: "Request origin is not allowed." }, { status: 403 });
  }

  const configuredAccessToken = process.env.SIGMAAGENT_WEB_ACCESS_TOKEN;
  if (!configuredAccessToken && process.env.NODE_ENV === "production") {
    return NextResponse.json(
      { error: "The live workspace is not enabled. Try SigmaAgent in Discord instead." },
      { status: 503 },
    );
  }
  if (configuredAccessToken) {
    const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") || "";
    const expectedBytes = Buffer.from(configuredAccessToken);
    const suppliedBytes = Buffer.from(supplied);
    if (expectedBytes.length !== suppliedBytes.length || !timingSafeEqual(expectedBytes, suppliedBytes)) {
      return NextResponse.json({ error: "A valid workspace access key is required." }, { status: 401 });
    }
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Send a valid JSON question." }, { status: 400 });
  }

  const question =
    body && typeof body === "object" && "question" in body
      ? (body as { question?: unknown }).question
      : undefined;
  if (typeof question !== "string" || question.trim().length < 3 || question.length > MAX_QUESTION_LENGTH) {
    return NextResponse.json(
      { error: `Enter a question between 3 and ${MAX_QUESTION_LENGTH} characters.` },
      { status: 400 },
    );
  }
  if (MUTATION_INTENT.test(question)) {
    return NextResponse.json(
      { error: "The web workspace is read-only. Use the connected Discord workflow for approved task updates." },
      { status: 400 },
    );
  }

  const configuredUrl = process.env.SIGMAAGENT_API_URL || process.env.INVESTIGATION_API_URL;
  const apiUrl = configuredUrl || "http://127.0.0.1:8000/investigate";
  let target: URL;
  try {
    target = new URL(apiUrl);
    if (!/^https?:$/.test(target.protocol)) throw new Error("Unsupported protocol");
  } catch {
    return NextResponse.json({ error: "The investigation service URL is invalid." }, { status: 500 });
  }

  const headers = new Headers({ "Content-Type": "application/json", Accept: "application/json" });
  const apiKey = process.env.INVESTIGATION_API_KEY;
  if (apiKey) headers.set("Authorization", `Bearer ${apiKey}`);

  try {
    const upstream = await fetch(target, {
      method: "POST",
      headers,
      body: JSON.stringify({ question: question.trim() }),
      cache: "no-store",
      signal: AbortSignal.timeout(45_000),
    });
    const payload = (await upstream.json().catch(() => ({}))) as BackendResponse;
    if (!upstream.ok) {
      const status = upstream.status === 404 || upstream.status === 503 ? 503 : upstream.status;
      return NextResponse.json(
        { error: status === 503 ? "SigmaAgent is unavailable right now. Check that the investigation API is running." : "SigmaAgent could not answer that question." },
        { status },
      );
    }
    if (typeof payload.answer !== "string") {
      return NextResponse.json({ error: "SigmaAgent returned an unexpected response." }, { status: 502 });
    }

    const sources = Array.isArray(payload.sources) ? payload.sources.filter(isSafeSource) : [];

    return NextResponse.json({ answer: payload.answer, sources });
  } catch (error) {
    const timedOut = error instanceof Error && error.name === "TimeoutError";
    return NextResponse.json(
      { error: timedOut ? "That took too long. Please try again." : "SigmaAgent could not reach the investigation service. Check that the API is running." },
      { status: 503 },
    );
  }
}
