// Writes capacitor.config.json from APP_URL so the APK always opens the deployed site.
// Usage: APP_URL=https://fanvaaz-xxxx.a.run.app npm run sync
import { writeFileSync } from "node:fs";

const url = process.env.APP_URL;
if (!url || !url.startsWith("https://")) {
  console.error("Set APP_URL to the HTTPS URL of the deployed Fanvaaz service.");
  process.exit(1);
}
const host = new URL(url).host;
const config = {
  appId: "app.fanvaaz.sov",
  appName: "Fanvaaz",
  webDir: "www",
  server: { url: `${url.replace(/\/$/, "")}/app`, cleartext: false, allowNavigation: [host] },
  android: { allowMixedContent: false, captureInput: true, webContentsDebuggingEnabled: false },
};
writeFileSync("capacitor.config.json", JSON.stringify(config, null, 2) + "\n");
console.log(`capacitor.config.json -> ${config.server.url}`);
