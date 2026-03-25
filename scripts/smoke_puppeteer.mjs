/**
 * Local browser smoke test (Puppeteer).
 *
 * Headless (CI / no display):
 *   npm run smoke
 *
 * Visible browser on your desktop (watch the window):
 *   npm run smoke:headed
 *
 * Env:
 *   HARO_SMOKE_URL   — page to open (default: HARO login URL)
 *   HARO_HEADED=1    — show real Chromium window (not headless)
 *   HARO_SLOW_MO_MS  — slow down actions (default 80 when headed, 0 headless)
 *   HARO_KEEP_OPEN_S — seconds to leave browser open before exit (default 20 headed, 0 headless)
 */
import puppeteer from "puppeteer";

const url = process.env.HARO_SMOKE_URL || "http://142.93.187.80:18080/login";
const expectTitle = "HARO Auto-Responder";
const headed =
  process.env.HARO_HEADED === "1" ||
  process.argv.includes("--headed") ||
  process.argv.includes("-x");
const slowMo = Number(process.env.HARO_SLOW_MO_MS ?? (headed ? "80" : "0"));
const keepOpenS = Number(
  process.env.HARO_KEEP_OPEN_S ?? (headed ? "20" : "0"),
);

console.log(
  `[smoke] mode=${headed ? "HEADED (visible window)" : "headless"} url=${url}`,
);

const browser = await puppeteer.launch({
  headless: headed ? false : "new",
  slowMo: slowMo || undefined,
  args: [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    ...(headed ? ["--window-size=1280,900", "--start-maximized"] : []),
  ],
  defaultViewport: headed ? null : { width: 1280, height: 900 },
});

try {
  const page = await browser.newPage();
  page.setDefaultNavigationTimeout(90000);
  console.log("[smoke] navigating…");
  const resp = await page.goto(url, { waitUntil: "networkidle2" });
  const status = resp ? resp.status() : 0;
  const title = await page.title();
  const ok = status >= 200 && status < 400 && title.includes(expectTitle);
  console.log(JSON.stringify({ url, status, title, ok }, null, 2));
  if (!ok) {
    process.exitCode = 1;
  }
  if (keepOpenS > 0 && headed) {
    console.log(
      `[smoke] leaving browser open for ${keepOpenS}s so you can inspect — close the window or wait.`,
    );
    await new Promise((r) => setTimeout(r, keepOpenS * 1000));
  }
} finally {
  await browser.close();
  console.log("[smoke] browser closed.");
}

process.exit(process.exitCode ?? 0);
