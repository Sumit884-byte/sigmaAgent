import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SigmaAgent — Understand what is blocking your project",
  description:
    "A project intelligence agent that connects tasks, dependencies, decisions, and team conversations.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
