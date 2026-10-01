"use client";

import React, { useEffect, useRef, useState } from "react";
import { useMarkets } from "@/hooks/useData";
import { useCompanies } from "@/hooks/useData";
import { FinGraphLogo, formatPrice, formatSigned, formatPct, getDirection, getArrow, cn } from "@/lib/utils";
import { GradientBarsBackground } from "@/components/ui/gradient-bars-background";
import { Footer } from "@/components/ui/footer-section";

const FEATURED_TICKERS = ["AAPL", "MSFT", "NVDA"];

export function LandingPage() {
  const { markets, isLoading: marketsLoading } = useMarkets();
  const { companies } = useCompanies();
  const [reducedMotion] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
  const [heroReady, setHeroReady] = useState(false);
  const tickerTrackRef = useRef<HTMLDivElement>(null);
  const [tickerItems, setTickerItems] = useState<React.ReactNode[]>([]);

  useEffect(() => {
    if (!reducedMotion) {
      const timer = setTimeout(() => setHeroReady(true), 100);
      return () => clearTimeout(timer);
    }
    setHeroReady(true);
  }, [reducedMotion]);

  // Render ticker tape
  useEffect(() => {
    if (!markets.length) return;
    const items: React.ReactNode[] = [];
    for (let pass = 0; pass < 2; pass++) {
      for (const row of markets) {
        const dir = getDirection(row.pct);
        items.push(
          <span key={`${pass}-${row.ticker}`} className="tick">
            <span className="tick__sym">{row.ticker}</span>
            <span className="tick__price">{formatPrice(row.price)}</span>
            <span className={cn("tick__chg", `is-${dir}`)}>
              {getArrow(dir)}
              {formatPct(row.pct)}
            </span>
          </span>
        );
      }
    }
    setTickerItems(items);
  }, [markets]);

  // Featured market cards
  const featuredMarkets = FEATURED_TICKERS.map((t) => markets.find((m) => m.ticker === t)).filter((m): m is NonNullable<typeof m> => Boolean(m));
  const otherMarkets = markets.filter((m) => !FEATURED_TICKERS.includes(m.ticker)).slice(0, 3);
  const displayMarkets = [...featuredMarkets, ...otherMarkets];

  // Company chips from graph
  const graphCompanies = companies.slice(0, 8);

  return (
    <div className="relative flex min-h-svh flex-col">
      <GradientBarsBackground className="fixed inset-0 z-0" />

      <a className="skip-link" href="#intro">
        Skip to content
      </a>

      <div className="relative z-10 page" id="page">
        {/* Floating navigation */}
        <header className="nav-wrap" id="nav-wrap">
          <nav className="nav" id="nav" aria-label="Primary">
            <a className="nav__brand" href="#top" aria-label="FinGraph — home">
              <FinGraphLogo size={32} className="logo" />
              <span className="nav__brand-name">FinGraph</span>
            </a>

            <div className="nav__links" id="nav-links" role="list">
              <a role="listitem" className="nav__link" href="#about">About</a>
              <a role="listitem" className="nav__link" href="#features">Features</a>
              <a role="listitem" className="nav__link" href="#how-it-works">How it works</a>
              <a role="listitem" className="nav__link" href="#insights">Insights</a>
              <a role="listitem" className="nav__link" href="#markets">Markets</a>
              <a role="listitem" className="nav__link" href="#pricing">Pricing</a>
            </div>

            <div className="nav__actions">
              <a className="btn btn--ghost btn--sm" href="/auth">Sign in</a>
              <a className="btn btn--primary btn--sm" href="/auth">Get Started</a>
            </div>

            <button className="nav__toggle" id="nav-toggle" type="button" aria-expanded="false" aria-controls="nav-panel" aria-label="Open the menu">
              <span className="nav__toggle-bar"></span>
              <span className="nav__toggle-bar"></span>
              <span className="nav__toggle-bar"></span>
            </button>
          </nav>

          {/* Mobile panel */}
          <div className="nav-panel" id="nav-panel" hidden>
            <div className="nav-panel__links" role="list">
              <a role="listitem" className="nav-panel__link" href="#about">About</a>
              <a role="listitem" className="nav-panel__link" href="#features">Features</a>
              <a role="listitem" className="nav-panel__link" href="#how-it-works">How it works</a>
              <a role="listitem" className="nav-panel__link" href="#insights">Insights</a>
              <a role="listitem" className="nav-panel__link" href="#markets">Markets</a>
              <a role="listitem" className="nav-panel__link" href="#pricing">Pricing</a>
            </div>
            <div className="nav-panel__actions">
              <a className="btn btn--ghost" href="/auth">Sign in</a>
              <a className="btn btn--primary" href="/auth">Get Started</a>
            </div>
          </div>
        </header>

        <main id="top">
          {/* Hero */}
          <section className={cn("hero", heroReady && "hero--ready")} id="hero" aria-labelledby="hero-title">
            <div className="hero__bg" aria-hidden="true" />
            <div className="hero__content">
              <p className="eyebrow">Financial intelligence&nbsp;/&nbsp;GraphRAG</p>

              <h1 className="hero__title" id="hero-title" aria-label="See the connections behind the numbers.">
                <span className="hero__title-line hero__title-line--top">
                  <span className="hero__title-prefix">See the</span>
                  <TextLoop />
                </span>
                <span className="hero__title-line hero__title-line--bottom">behind the numbers.</span>
              </h1>

              <p className="hero__lede">
                Explore financial data, company relationships, filings, and market
                signals through an intelligent knowledge graph.
              </p>

              <div className="hero__cta">
                <a className="btn btn--primary btn--lg" href="/auth">
                  <span className="btn__label">Explore FinGraph</span>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                    <path d="M5 12h13M13 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </a>
                <a className="btn btn--ghost btn--lg" href="#intro">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                    <path d="M8 5l10 7-10 7V5Z" strokeLinejoin="round" />
                  </svg>
                  <span className="btn__label">See how it works</span>
                </a>
              </div>
            </div>
          </section>

          {/* Intro statement */}
          <section className="intro" id="intro" aria-labelledby="intro-title">
            <div className="section-head">
              <h2 className="section-head__title" id="intro-title">
                Financial data is connected.<br />
                Your analysis should be too.
              </h2>
              <p className="section-head__sub">
                FinGraph connects financial metrics, company relationships, filings,
                events, and market data into one explorable intelligence layer.
              </p>
            </div>

            <div className="intro__cards">
              <article className="num-card">
                <span className="num-card__index" aria-hidden="true">01</span>
                <h3 className="num-card__title">Connected data</h3>
                <p className="num-card__body">
                  Metrics, relationships and filings live in one graph that keeps
                  every connection explicit.
                </p>
              </article>

              <article className="num-card">
                <span className="num-card__index" aria-hidden="true">02</span>
                <h3 className="num-card__title">Evidence-ground answers</h3>
                <p className="num-card__body">
                  Answers are traced back to the filings and figures they draw on,
                  with the path shown, not hidden.
                </p>
              </article>

              <article className="num-card">
                <span className="num-card__index" aria-hidden="true">03</span>
                <h3 className="num-card__title">Relationship-based insights</h3>
                <p className="num-card__body">
                  Understand how a company, its segments, its geography and its
                  reporting fit together — as a shape, not a spreadsheet.
                </p>
              </article>
            </div>
          </section>

          {/* Markets */}
          <section className="markets" id="markets" aria-labelledby="markets-title">
            <div className="section-head">
              <h2 className="section-head__title" id="markets-title">
                Live markets,<br />
                straight from the source.
              </h2>
              <p className="section-head__sub">
                Real-time quotes from Yahoo Finance — the same tickers FinGraph
                grounds its answers in, plus the names that move the market.
              </p>
            </div>

            <div className="ticker" id="ticker" aria-label="Live market ticker">
              <div className="ticker__track" id="ticker-track" ref={tickerTrackRef}>
                {tickerItems}
              </div>
            </div>

            <div className="markets__grid" id="markets-grid" aria-live="off">
              {marketsLoading ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <article key={i} className="mkt-card skeleton">
                    <div className="mkt-card__head">
                      <span className="mkt-card__sym skeleton-text" />
                      <span className="mkt-card__price skeleton-text" />
                    </div>
                    <span className="mkt-card__chg skeleton-text" />
                  </article>
                ))
              ) : (
                displayMarkets.map((row) => {
                  const dir = getDirection(row.pct);
                  return (
                    <article key={row.ticker} className="mkt-card">
                      <div className="mkt-card__head">
                        <span className="mkt-card__sym">{row.ticker}</span>
                        <span className="mkt-card__price">{formatPrice(row.price)}</span>
                      </div>
                      <span className={cn("mkt-card__chg", `is-${dir}`)}>
                        {getArrow(dir)}
                        <span>{formatSigned(row.change)} ({formatPct(row.pct)})</span>
                        <small>today</small>
                      </span>
                    </article>
                  );
                })
              )}
            </div>

            <p className="markets__status" id="markets-status" role="status" aria-live="polite">
              {marketsLoading && "Loading live quotes…"}
            </p>
          </section>

          {/* Companies in Graph */}
          {companies.length > 0 && (
            <section className="companies" id="companies" aria-labelledby="companies-title">
              <div className="section-head">
                <h2 className="section-head__title" id="companies-title">
                  Companies in the Graph
                </h2>
                <p className="section-head__sub">
                  {companies.length} issuer{companies.length !== 1 ? "s" : ""} with {companies.reduce((sum, c) => sum + c.filings, 0)} filing{companies.reduce((sum, c) => sum + c.filings, 0) !== 1 ? "s" : ""} loaded.
                </p>
              </div>
              <div className="companies__chips" role="list">
                {graphCompanies.map((company) => (
                  <a
                    key={company.ticker}
                    className="company-chip"
                    href={`/company/${company.ticker}`}
                    role="listitem"
                    aria-label={`${company.name} (${company.ticker}) — ${company.filings} filing${company.filings !== 1 ? "s" : ""}`}
                  >
                    <span className="company-chip__ticker">{company.ticker}</span>
                    <span className="company-chip__name">{company.name}</span>
                    <span className="company-chip__count">{company.filings} filing{company.filings !== 1 ? "s" : ""}</span>
                  </a>
                ))}
              </div>
            </section>
          )}

          {/* Pricing */}
          <section className="plans" id="pricing" aria-labelledby="pricing-title">
            <div className="section-head">
              <h2 className="section-head__title" id="pricing-title">
                Plans that fit<br />your analysis.
              </h2>
              <p className="section-head__sub">
                Choose the FinGraph plan that matches your financial intelligence needs.
              </p>
            </div>

            <BillingToggle />

            <div className="plans__grid">
              <PricingPlan
                plan="free"
                name="Free"
                blurb="For exploring the graph on your own."
                monthly={0}
                yearly={0}
                features={[
                  "3 questions per day",
                  "Single-issuer graph view",
                  "Community support",
                  "Public filings only",
                ]}
                cta="Start for free"
                ctaVariant="ghost"
              />
              <PricingPlan
                plan="pro"
                name="Pro"
                blurb="For analysts who live in the data."
                monthly={29}
                yearly={23}
                badge="Most Popular"
                features={[
                  "Unlimited questions",
                  "Multi-issuer comparison",
                  "Live market data & signals",
                  "Full filing history & traces",
                  "Priority support",
                ]}
                cta="Get Started"
                ctaVariant="primary"
                highlighted
              />
              <PricingPlan
                plan="enterprise"
                name="Enterprise"
                blurb="For teams and institutions."
                monthly={0}
                yearly={0}
                custom
                features={[
                  "Everything in Pro",
                  "Private graph deployment",
                  "SSO & audit logs",
                  "Dedicated data pipeline",
                  "Custom integrations & SLA",
                ]}
                cta="Contact Sales"
                ctaVariant="ghost"
              />
            </div>

            <p className="plans__note">
              All plans include read-only access to the public knowledge graph.
              No credit card required to start.
            </p>
          </section>
        </main>

        <Footer />
      </div>

      <p className="sr-only" id="announcer" role="status" aria-live="polite" />
    </div>
  );
}

function TextLoop() {
  const words = ["connections", "relationships", "patterns", "signals", "insights"];
  const [index, setIndex] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => {
      setIndex((i) => (i + 1) % words.length);
    }, 2500);
    return () => clearInterval(timer);
  }, []);

  return (
    <span className="text-loop" aria-hidden="true">
      <span className="text-loop__spacer">relationships</span>
      <span className="text-loop__inner">
        {words.map((word, i) => (
          <span key={word} className={cn("text-loop__word", i === index && "is-active")}>
            {word}
          </span>
        ))}
      </span>
    </span>
  );
}

function BillingToggle() {
  const [yearly, setYearly] = useState(false);

  return (
    <div className="billing-toggle" role="group" aria-label="Billing period">
      <span className={cn("billing-toggle__label", !yearly && "is-on")} id="label-monthly">
        Monthly
      </span>
      <button
        className="billing-toggle__switch"
        id="billing-switch"
        type="button"
        role="switch"
        aria-checked={yearly}
        aria-labelledby="label-monthly label-yearly"
        onClick={() => setYearly((y) => !y)}
      >
        <span className="billing-toggle__thumb" aria-hidden="true" />
      </button>
      <span className={cn("billing-toggle__label", yearly && "is-on")} id="label-yearly">
        Yearly
      </span>
      <span className="billing-toggle__save" aria-hidden="true">Save 20%</span>
    </div>
  );
}

interface PricingPlanProps {
  plan: "free" | "pro" | "enterprise";
  name: string;
  blurb: string;
  monthly: number;
  yearly: number;
  badge?: string;
  features: string[];
  cta: string;
  ctaVariant: "primary" | "ghost";
  highlighted?: boolean;
  custom?: boolean;
}

function PricingPlan({
  plan,
  name,
  blurb,
  monthly,
  yearly: yearlyPrice,
  badge,
  features,
  cta,
  ctaVariant,
  highlighted,
  custom,
}: PricingPlanProps) {
  const [isYearly] = useState(false);
  // In a real app, this would be synced via context. For now, local state.

  return (
    <article className={cn("plan", highlighted && "plan--pro")} data-plan={plan}>
      {badge && <span className="plan__badge">{badge}</span>}

      <header className="plan__head">
        <h2 className="plan__name">{name}</h2>
        <p className="plan__blurb">{blurb}</p>
      </header>

      {!custom ? (
        <>
          <p className="plan__price">
            <span
              className="plan__amount"
              data-monthly={monthly}
              data-yearly={yearlyPrice}
            >
              ${isYearly ? yearlyPrice : monthly}
            </span>
            <span className="plan__period">/ month</span>
          </p>
          <p className="plan__billing-note" data-billing-note>
            {isYearly ? "Billed yearly" : "Billed monthly"}
          </p>
        </>
      ) : (
        <>
          <p className="plan__price">
            <span className="plan__amount plan__amount--custom">Custom</span>
          </p>
          <p className="plan__billing-note" data-billing-note>Tailored to your stack</p>
        </>
      )}

      <ul className="plan__features">
        {features.map((feature, i) => (
          <li key={i}>{feature}</li>
        ))}
      </ul>

      <a className={cn("btn", `btn--${ctaVariant}`, "plan__cta")} href="/auth">
        {cta}
      </a>
    </article>
  );
}