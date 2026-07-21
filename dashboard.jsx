import React, { useState, useEffect, useRef, useCallback } from "react";

/**
 * MCA Operator Terminal
 *
 * Design brief: this is not a portfolio app. Its single job is to let one
 * operator see how much risk is live and stop it in one action. So the layout
 * is inverted from the usual trading dashboard: equity is a small number in the
 * header, and the risk budget gauges get a full-width row of their own.
 *
 * Palette is a cold laboratory instrument, not a hacker terminal. The one
 * saturated colour, amber, is reserved exclusively for risk state — nothing
 * else on the page is allowed to use it. Losses render in a desaturated rust,
 * because on a meme-coin desk red is the *normal* state, and a screen that
 * screams at every drawdown trains the operator to ignore it.
 *
 * Signature element: the slot ribbon under the header. One hairline per event,
 * scrolling right to left, coloured by class. It encodes throughput and lag —
 * the thing that actually determines whether this strategy works.
 */

const C = {
  ground: "#0F1317",
  panel: "#161B21",
  panelHi: "#1B222A",
  rule: "#28313A",
  bone: "#E3E7E4",
  muted: "#78838D",
  faint: "#4A555F",
  amber: "#E8A33D",   // risk state ONLY
  cyan: "#4FB6C0",    // confirmed / live
  rust: "#B0563F",    // loss
  moss: "#7A9A6B",    // gain, deliberately quiet
};

const FONTS = `
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;900&family=Martian+Mono:wght@300;400;600&display=swap');
`;

const mono = { fontFamily: "'Martian Mono', ui-monospace, monospace", fontVariantNumeric: "tabular-nums" };
const display = { fontFamily: "'Archivo', system-ui, sans-serif" };

/* ------------------------------------------------------------------ */
/* Simulated feed. Replace with the /ws WebSocket in mca.api.app.      */
/* ------------------------------------------------------------------ */

const SYMBOLS = ["WOJAK", "BONKR", "PEPE2", "MOGGY", "SIGMA", "GROK9", "SNEK", "CHONK", "BASED", "TURBO"];
const CHAINS = ["SOL", "BASE", "BSC"];
const GATES = ["mint_authority", "lp_locked", "top10_holders", "sell_simulation", "min_liquidity", "volume_liquidity_sanity", "deployer_history", "min_age"];

const rnd = (a, b) => a + Math.random() * (b - a);
const pick = (a) => a[Math.floor(Math.random() * a.length)];

function makeSignal(id) {
  const passed = Math.random() > 0.82;
  const score = passed ? rnd(62, 94) : rnd(0, 44);
  const nFailed = passed ? 0 : 1 + Math.floor(Math.random() * 2);
  return {
    id,
    sym: pick(SYMBOLS),
    chain: pick(CHAINS),
    score,
    passed,
    failed: passed ? [] : Array.from({ length: nFailed }, () => pick(GATES)),
    pWin: passed ? rnd(0.34, 0.61) : null,
    ev: passed ? rnd(-180, 640) : null,
    ts: Date.now(),
  };
}

function makePosition(id) {
  const entry = rnd(0.00008, 0.004);
  const pnl = rnd(-0.28, 2.4);
  const rungs = [0.45, 1.0, 2.0, 4.0];
  return {
    id,
    sym: pick(SYMBOLS),
    chain: pick(CHAINS),
    size: rnd(120, 740),
    entry,
    pnl,
    rungs: rungs.map((r) => ({ at: r, done: pnl >= r })),
    stop: pnl > 0.3 ? entry * 1.02 : entry * 0.75,
    heldMin: Math.floor(rnd(2, 180)),
    score: rnd(70, 93),
  };
}

/* ------------------------------------------------------------------ */
/* Signature: the slot ribbon                                          */
/* ------------------------------------------------------------------ */

function SlotRibbon({ ticks, reduced }) {
  return (
    <div
      aria-label="Event throughput, most recent on the right"
      style={{
        height: 34, background: C.panel, borderBottom: `1px solid ${C.rule}`,
        display: "flex", alignItems: "flex-end", gap: 2, padding: "0 16px 6px",
        overflow: "hidden", position: "relative",
      }}
    >
      {ticks.map((t, i) => (
        <div
          key={t.k}
          style={{
            width: 2,
            height: 6 + t.h * 20,
            background: t.c,
            opacity: reduced ? 0.75 : 0.35 + (i / ticks.length) * 0.65,
            transition: reduced ? "none" : "height 120ms linear",
            flexShrink: 0,
          }}
        />
      ))}
      <span style={{
        ...mono, position: "absolute", right: 16, bottom: 6, fontSize: 9,
        color: C.faint, letterSpacing: "0.08em", background: C.panel, paddingLeft: 8,
      }}>
        NOW
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Signature: exit ladder glyph                                        */
/* ------------------------------------------------------------------ */

function LadderGlyph({ rungs }) {
  return (
    <div style={{ display: "flex", gap: 3, alignItems: "center" }}
         aria-label={`${rungs.filter(r => r.done).length} of ${rungs.length} exit rungs filled`}>
      {rungs.map((r, i) => (
        <div key={i} style={{
          width: 14, height: 3,
          background: r.done ? C.cyan : C.rule,
          opacity: r.done ? 1 : 0.6,
        }} />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function BudgetGauge({ label, used, limit, unit = "%" }) {
  const frac = Math.min(used / limit, 1);
  const critical = frac > 0.7;
  return (
    <div style={{ flex: 1, minWidth: 128 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <span style={{ ...mono, fontSize: 9, color: C.muted, letterSpacing: "0.1em" }}>
          {label}
        </span>
        <span style={{ ...mono, fontSize: 10, color: critical ? C.amber : C.bone }}>
          {used.toFixed(1)}/{limit}{unit}
        </span>
      </div>
      <div style={{ height: 4, background: C.rule, position: "relative" }}>
        <div style={{
          height: "100%", width: `${frac * 100}%`,
          background: critical ? C.amber : C.faint,
          transition: "width 400ms ease-out",
        }} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

export default function OperatorTerminal() {
  const [reduced, setReduced] = useState(false);
  const [armed, setArmed] = useState(false);
  const [halted, setHalted] = useState(false);
  const [signals, setSignals] = useState(() => Array.from({ length: 9 }, (_, i) => makeSignal(i)));
  const [positions, setPositions] = useState(() => Array.from({ length: 3 }, (_, i) => makePosition(i)));
  const [ticks, setTicks] = useState(() =>
    Array.from({ length: 90 }, (_, k) => ({ k, h: Math.random(), c: C.faint })));
  const [equity, setEquity] = useState(10412.55);
  const idRef = useRef(1000);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const on = (e) => setReduced(e.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  useEffect(() => {
    if (halted) return;
    const iv = setInterval(() => {
      setTicks((prev) => {
        const roll = Math.random();
        const c = roll > 0.94 ? C.cyan : roll > 0.72 ? C.bone : C.faint;
        return [...prev.slice(1), { k: idRef.current++, h: Math.random(), c }];
      });
      if (Math.random() > 0.75) {
        setSignals((prev) => [makeSignal(idRef.current++), ...prev].slice(0, 9));
      }
      setPositions((prev) => prev.map((p) => ({
        ...p,
        pnl: p.pnl + rnd(-0.035, 0.04),
        rungs: p.rungs.map((r) => ({ ...r, done: r.done || p.pnl >= r.at })),
      })));
      setEquity((e) => e + rnd(-14, 16));
    }, reduced ? 1600 : 420);
    return () => clearInterval(iv);
  }, [halted, reduced]);

  const kill = useCallback(() => { setHalted(true); setArmed(false); }, []);

  const gross = positions.reduce((s, p) => s + p.size, 0);
  const riskState = halted ? "HALTED" : armed ? "LIVE" : "PAPER";
  const riskColor = halted ? C.amber : armed ? C.amber : C.cyan;

  return (
    <div style={{ background: C.ground, minHeight: "100vh", color: C.bone }}>
      <style>{FONTS}</style>
      <style>{`
        *:focus-visible { outline: 2px solid ${C.cyan}; outline-offset: 2px; }
        .row:hover { background: ${C.panelHi}; }
      `}</style>

      {/* ---- instrument header ---- */}
      <header style={{
        display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap",
        padding: "14px 16px", background: C.panel, borderBottom: `1px solid ${C.rule}`,
      }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <span style={{ ...display, fontWeight: 900, fontSize: 15, letterSpacing: "-0.02em" }}>
            MCA
          </span>
          <span style={{
            ...mono, fontSize: 9, letterSpacing: "0.16em", color: riskColor,
            border: `1px solid ${riskColor}`, padding: "2px 6px",
          }}>
            {riskState}
          </span>
        </div>

        <div style={{ display: "flex", gap: 28, flexWrap: "wrap", flex: 1 }}>
          <Stat label="EQUITY" value={`$${equity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} />
          <Stat label="GROSS" value={`$${gross.toFixed(0)}`} />
          <Stat label="OPEN" value={`${positions.length}/5`} />
          <Stat label="LAG p99" value="1.24s" tone={C.muted} />
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => setArmed((a) => !a)}
            disabled={halted}
            style={btn(armed ? C.amber : C.rule, armed ? C.ground : C.bone, armed ? C.amber : "transparent", halted)}
          >
            {armed ? "Disarm" : "Arm live"}
          </button>
          <button onClick={kill} style={btn(C.rust, C.bone, C.rust, false)}>
            Halt all
          </button>
        </div>
      </header>

      <SlotRibbon ticks={ticks} reduced={reduced} />

      {/* ---- two columns ---- */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 1, background: C.rule }}>

        {/* signal feed */}
        <section style={{ background: C.ground, padding: "16px 0 20px" }}>
          <SectionHead title="Signal feed" note="rejections included by design" />
          {signals.map((s) => (
            <div key={s.id} className="row" style={rowStyle}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 118 }}>
                <span style={{ ...mono, fontSize: 8, color: C.faint, width: 30 }}>{s.chain}</span>
                <span style={{ ...display, fontWeight: 700, fontSize: 13 }}>{s.sym}</span>
              </div>

              <div style={{ flex: 1, minWidth: 90 }}>
                <div style={{ height: 3, background: C.rule, position: "relative" }}>
                  <div style={{
                    height: "100%", width: `${s.score}%`,
                    background: s.passed ? C.cyan : C.faint,
                  }} />
                  <div style={{
                    position: "absolute", left: "70%", top: -3, width: 1, height: 9,
                    background: C.muted,
                  }} title="threshold 70" />
                </div>
              </div>

              <span style={{ ...mono, fontSize: 11, width: 38, textAlign: "right",
                             color: s.passed ? C.bone : C.faint }}>
                {s.score.toFixed(0)}
              </span>

              <span style={{ ...mono, fontSize: 9, width: 118, textAlign: "right",
                             color: s.passed ? (s.ev > 250 ? C.moss : C.muted) : C.faint,
                             overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {s.passed ? `EV ${s.ev.toFixed(0)}bps` : s.failed[0]}
              </span>
            </div>
          ))}
        </section>

        {/* positions */}
        <section style={{ background: C.ground, padding: "16px 0 20px" }}>
          <SectionHead title="Open positions" note="rungs fill left to right" />
          {positions.map((p) => {
            const up = p.pnl >= 0;
            return (
              <div key={p.id} className="row" style={{ ...rowStyle, gap: 12 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 118 }}>
                  <span style={{ ...mono, fontSize: 8, color: C.faint, width: 30 }}>{p.chain}</span>
                  <span style={{ ...display, fontWeight: 700, fontSize: 13 }}>{p.sym}</span>
                </div>

                <span style={{ ...mono, fontSize: 9, color: C.muted, width: 44 }}>
                  ${p.size.toFixed(0)}
                </span>

                <LadderGlyph rungs={p.rungs} />

                <span style={{ ...mono, fontSize: 9, color: C.faint, width: 40, textAlign: "right" }}>
                  {p.heldMin}m
                </span>

                <span style={{ ...mono, fontSize: 12, width: 66, textAlign: "right",
                               color: up ? C.moss : C.rust }}>
                  {up ? "+" : ""}{(p.pnl * 100).toFixed(0)}%
                </span>
              </div>
            );
          })}
          {positions.length === 0 && (
            <p style={{ ...mono, fontSize: 10, color: C.muted, padding: "20px 16px" }}>
              No open positions. The gate is rejecting everything, or nothing scored above 70.
            </p>
          )}
        </section>
      </div>

      {/* ---- risk budget: full width, deliberately the largest row ---- */}
      <section style={{
        background: C.panel, borderTop: `1px solid ${C.rule}`,
        padding: "18px 16px 22px",
      }}>
        <div style={{ ...mono, fontSize: 9, color: C.muted, letterSpacing: "0.14em", marginBottom: 14 }}>
          RISK BUDGET
        </div>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <BudgetGauge label="DAILY LOSS" used={2.4} limit={6} />
          <BudgetGauge label="WEEKLY LOSS" used={5.1} limit={12} />
          <BudgetGauge label="GROSS EXPOSURE" used={(gross / equity) * 100} limit={15} />
          <BudgetGauge label="DRAWDOWN FROM PEAK" used={3.8} limit={20} />
          <BudgetGauge label="CONSEC. LOSSES" used={2} limit={6} unit="" />
        </div>
        <p style={{ ...mono, fontSize: 9, color: C.faint, marginTop: 16, maxWidth: 620, lineHeight: 1.7 }}>
          Any gauge reaching its limit moves the system to LOCKED. Clearing LOCKED needs a
          TOTP code and a written justification, and is recorded permanently.
        </p>
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ */

const rowStyle = {
  display: "flex", alignItems: "center", gap: 10,
  padding: "9px 16px", borderBottom: `1px solid ${C.panel}`,
};

function Stat({ label, value, tone }) {
  return (
    <div>
      <div style={{ ...mono, fontSize: 8, color: C.faint, letterSpacing: "0.14em", marginBottom: 3 }}>
        {label}
      </div>
      <div style={{ ...mono, fontSize: 13, color: tone || C.bone }}>{value}</div>
    </div>
  );
}

function SectionHead({ title, note }) {
  return (
    <div style={{
      display: "flex", alignItems: "baseline", justifyContent: "space-between",
      padding: "0 16px 12px",
    }}>
      <h2 style={{ ...display, fontWeight: 700, fontSize: 11, letterSpacing: "0.14em", margin: 0 }}>
        {title.toUpperCase()}
      </h2>
      <span style={{ ...mono, fontSize: 8, color: C.faint }}>{note}</span>
    </div>
  );
}

function btn(bg, fg, border, disabled) {
  return {
    ...mono, fontSize: 10, letterSpacing: "0.08em",
    padding: "7px 14px", cursor: disabled ? "not-allowed" : "pointer",
    background: bg === C.rule ? "transparent" : bg,
    color: fg, border: `1px solid ${border === "transparent" ? C.rule : border}`,
    opacity: disabled ? 0.4 : 1,
  };
}
