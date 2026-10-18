import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";

export const metadata: Metadata = {
  title: "SigmaAgent — Understand how work connects",
  description:
    "Understand what is blocking work, why it happened, and which conversation or decision explains what to do next.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <Script id="sigma-theme-init" strategy="beforeInteractive">
          {`try { document.documentElement.dataset.theme = localStorage.getItem("sigma-theme") === "light" ? "light" : "dark"; } catch { document.documentElement.dataset.theme = "dark"; }`}
        </Script>
        {children}
      </body>
    </html>
  );
}
