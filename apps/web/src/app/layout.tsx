import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "QuotePilot AI",
  description: "RFQ to verified quotation for B2B distributors",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
