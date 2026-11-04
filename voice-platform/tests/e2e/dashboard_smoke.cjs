// Browser smoke test for the admin dashboard (run by tests/e2e/test_dashboard.py).
const { chromium } = require("playwright");

(async () => {
  const { VP_URL, VP_EMAIL, VP_PASSWORD, VP_CONFIG } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error" && !/status of (401|422)/.test(m.text())) errors.push(m.text()); });
  const step = async (name, fn) => {
    try { await fn(); } catch (e) {
      const main = await page.textContent("main").catch(() => "");
      console.error(`STEP FAILED: ${name}\n${e}\nMAIN: ${(main || "").slice(0, 1500)}`);
      await browser.close(); process.exit(1);
    }
  };
  await step("login", async () => {
    await page.goto(VP_URL + "/dashboard/");
    await page.fill("input[name=email]", VP_EMAIL);
    await page.fill("input[name=password]", VP_PASSWORD);
    await page.click("form#login button");
    await page.waitForSelector("text=Overview (last 30 days)");
  });
  await step("create agent", async () => {
    await page.click("nav >> text=Agents");
    await page.waitForSelector("#a-config");
    await page.fill("#a-slug", "ui-agent");
    await page.fill("#a-name", "UI Agent");
    await page.fill("#a-config", VP_CONFIG);
    await page.click("#a-validate");
    await page.waitForSelector(".notice >> text=Valid");
    await page.click("#a-create");
    await page.waitForSelector("h2 >> text=UI Agent");
  });
  await step("publish + deploy", async () => {
    await page.click("#v-publish");
    await page.waitForSelector("td >> text=published");
    await page.waitForLoadState("networkidle");
    await page.selectOption("#d-env", "staging");
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/deployments/staging") && r.request().method() === "PUT"),
      page.click("#d-deploy"),
    ]);
    await page.waitForLoadState("networkidle");
    await page.waitForFunction(() => [...document.querySelectorAll("main tr")].some(
      (tr) => tr.cells[0]?.textContent === "staging" && tr.cells[1]?.textContent === "v1"));
  });
  await step("validation errors surface", async () => {
    await page.fill("#v-config", JSON.stringify({ name: "broken" }));
    await page.click("#v-save");
    await page.waitForSelector(".error >> text=invalid agent configuration");
  });
  for (const view of ["Conversations", "Knowledge", "Integrations", "Phone numbers", "Evaluations", "Optimizations", "Settings", "Overview"]) {
    await step(`view ${view}`, async () => {
      await page.click(`nav >> text=${view}`);
      await page.waitForSelector("main h2");
      await page.waitForTimeout(150);
      if (await page.$("main .error")) throw new Error(await page.textContent("main .error"));
    });
  }
  await browser.close();
  if (errors.length) { console.error("browser errors:\n" + errors.join("\n")); process.exit(1); }
  console.log("dashboard smoke test passed");
})();
