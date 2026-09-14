import type { Metadata, Viewport } from "next";
import { Inter, Noto_Sans_Bengali, Noto_Sans_Devanagari } from "next/font/google";
import { Providers, THEME_SCRIPT } from "./providers";
import SiteHeader from "./components/SiteHeader";
import SiteFooter from "./components/SiteFooter";
import "./globals.css";

/**
 * Three families, and only one of them is ever downloaded by default.
 *
 * Inter carries the Latin UI. The two Noto faces exist because the corpus is
 * full of Devanagari and Bengali — scheme names, eligibility text, the whole
 * translated card — and v1 loaded `subsets: ["latin"]` only, so every Hindi and
 * Bengali glyph on the page fell back to whatever the OS happened to have. On a
 * cheap Android that is frequently a face with no matching weights and wildly
 * different metrics, which is why translated cards looked broken rather than
 * merely different.
 *
 * `preload: false` is what keeps that from costing anything: the browser fetches
 * an Indic face only when it has to actually shape a glyph from it, so an
 * English-only session over 3G pays for Inter and nothing else. The plan makes
 * bundle size on a low-end phone a product requirement, not a nicety.
 *
 * No `weight` on any of them — all three are variable fonts, so one file covers
 * 400 through 700. v1 asked for five static weights of Inter, which is five
 * files for a range one file already contains.
 */
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

const devanagari = Noto_Sans_Devanagari({
  subsets: ["devanagari"],
  display: "swap",
  preload: false,
  variable: "--font-devanagari",
});

const bengali = Noto_Sans_Bengali({
  subsets: ["bengali"],
  display: "swap",
  preload: false,
  variable: "--font-bengali",
});

export const metadata: Metadata = {
  title: {
    default: "Bhasha — find the government schemes you qualify for",
    template: "%s · Bhasha",
  },
  description:
    "Ask about government welfare schemes in your own language, by voice or by " +
    "typing. Bhasha searches 1,786 central and state schemes and tells you what " +
    "you are eligible for.",
  applicationName: "Bhasha",
  formatDetection: { telephone: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Pinch-zoom stays available. Capping it is a common "app-like" default and it
  // is an accessibility failure for exactly the people this product is for.
  maximumScale: 5,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f8f6f1" },
    { media: "(prefers-color-scheme: dark)", color: "#1b1c24" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${inter.variable} ${devanagari.variable} ${bengali.variable} h-full antialiased`}
    >
      <head>
        {/* Sets `.dark` before first paint. `suppressHydrationWarning` above is
            required and not a shrug: this script mutates the class on <html>
            before React hydrates, so the server's markup and the client's DOM
            legitimately differ on that one attribute. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="flex min-h-full flex-col">
        {/* Every page is one tab-stop from its content. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-100 focus:rounded-xl focus:bg-primary focus:px-4 focus:py-3 focus:font-semibold focus:text-on-primary"
        >
          Skip to content
        </a>
        <Providers>
          <SiteHeader />
          <main id="main" className="flex-1">
            {children}
          </main>
          <SiteFooter />
        </Providers>
      </body>
    </html>
  );
}
