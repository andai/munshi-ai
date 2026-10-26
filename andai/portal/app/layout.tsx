import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { ThemeScript } from "@/components/theme/theme-script";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "munshi.ai", template: "%s · munshi.ai" },
  description: "munshi.ai — meeting notes for your organisation",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FAFAFD" },
    { media: "(prefers-color-scheme: dark)", color: "#121120" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <ThemeScript />
      </head>
      <body className="min-h-dvh antialiased">{children}</body>
    </html>
  );
}
