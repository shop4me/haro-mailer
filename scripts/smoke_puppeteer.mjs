/**
 * Local browser smoke test (Puppeteer). Run: npm run smoke
 * Base URL: HARO_SMOKE_URL or default below.
 */
import puppeteer from "puppeteer";

const url = process.env.HARO_SMOKE_URL || "http://142.93.187.80:18080/login";
const expectTitle = "HARO Auto-Responder";

const browser = await puppeteer.launch({
  headless: "new",
  args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
});

try {
  const page = await browser.newPage();
  page.setDefaultNavigationTimeout(60000);
  const resp = await page.goto(url, { waitUntil: "networkidle2" });
  const status = resp ? resp.status() : 0;
  const title = await page.title();
  const ok = status >= 200 && status < 400 && title.includes(expectTitle);
  console.log(JSON.stringify({ url, status, title, ok }, null, 2));
  if (!ok) {
    process.exit(1);
  }
} finally {
  await browser.close();
}
