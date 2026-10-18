import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

function validDiscordInvite(raw: string): URL | null {
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:") return null;
    const host = url.hostname.toLowerCase();
    const discordHost = host === "discord.gg" || host === "www.discord.gg" || host === "discord.com" || host === "www.discord.com";
    if (!discordHost) return null;

    if ((host === "discord.gg" || host === "www.discord.gg") && /^\/[a-z\d-]+\/?$/i.test(url.pathname)) return url;
    if ((host === "discord.com" || host === "www.discord.com") && /^\/invite\/[a-z\d-]+\/?$/i.test(url.pathname)) return url;
    if (url.pathname === "/oauth2/authorize" && /^\d{17,20}$/.test(url.searchParams.get("client_id") || "")) return url;
  } catch {
    return null;
  }
  return null;
}

export function GET(request: NextRequest) {
  const configuredInvite = process.env.DISCORD_INVITE_URL || process.env.NEXT_PUBLIC_DISCORD_INVITE_URL;
  const invite = configuredInvite ? validDiscordInvite(configuredInvite) : null;
  if (invite) return NextResponse.redirect(invite, 307);

  const applicationId = process.env.DISCORD_APPLICATION_ID;
  if (applicationId && /^\d{17,20}$/.test(applicationId)) {
    const installUrl = new URL("https://discord.com/oauth2/authorize");
    installUrl.searchParams.set("client_id", applicationId);
    installUrl.searchParams.set("scope", "bot applications.commands");
    installUrl.searchParams.set("permissions", "68608");
    return NextResponse.redirect(installUrl, 307);
  }

  return NextResponse.redirect(new URL("/discord/setup", request.url), 307);
}
