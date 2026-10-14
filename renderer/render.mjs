// Renders a batch of PersonalityCard clips: `node render.mjs plan.json`.
//
// plan.json: { "imagesDir": "/abs/dir", "concurrency": null | number,
//              "items": [{ "image": "1.1.jpg", "props": { name, years, ... }, "output": "/abs/1.1.mp4" }] }
//
// The project is bundled and Chrome is opened once per batch. Images are served from
// imagesDir over a local HTTP server, so jobs never share files in public/.
// Prints "PROGRESS render <done>/<total>" after every clip; a failed clip is reported
// ("FAILED <output> <reason>") and the batch goes on. Existing outputs are skipped.

import { createReadStream, existsSync, readFileSync, renameSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { bundle } from "@remotion/bundler";
import { openBrowser, renderMedia, selectComposition } from "@remotion/renderer";
import { enableTailwind } from "@remotion/tailwind-v4";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const COMPOSITION = "PersonalityCard";

const plan = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const imagesDir = path.resolve(plan.imagesDir);

const server = createServer((req, res) => {
  const file = path.join(imagesDir, decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!file.startsWith(imagesDir + path.sep) || !existsSync(file)) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { "Content-Type": "image/jpeg", "Access-Control-Allow-Origin": "*" });
  createReadStream(file).pipe(res);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const imageBase = `http://127.0.0.1:${server.address().port}/`;

const todo = plan.items.filter((item) => !existsSync(item.output));
let done = plan.items.length - todo.length;
console.log(`PROGRESS render ${done}/${plan.items.length}`);

let browser;
try {
  if (todo.length) {
    const serveUrl = await bundle({
      entryPoint: path.join(ROOT, "src/index.ts"),
      publicDir: path.join(ROOT, "public"),
      rspack: true,
      bundlerOverride: enableTailwind,
    });
    browser = await openBrowser("chrome");
    for (const item of todo) {
      const inputProps = { ...item.props, image: imageBase + encodeURIComponent(item.image) };
      const partial = item.output.replace(/\.mp4$/, ".part.mp4");
      try {
        const composition = await selectComposition({ serveUrl, id: COMPOSITION, inputProps, puppeteerInstance: browser });
        await renderMedia({
          composition,
          serveUrl,
          codec: "h264",
          imageFormat: "jpeg",
          inputProps,
          outputLocation: partial,
          puppeteerInstance: browser,
          concurrency: plan.concurrency ?? null,
          overwrite: true,
        });
        renameSync(partial, item.output);
      } catch (error) {
        console.log(`FAILED ${item.output} ${String(error?.message ?? error).split("\n")[0]}`);
      }
      done += 1;
      console.log(`PROGRESS render ${done}/${plan.items.length}`);
    }
  }
} finally {
  await browser?.close({ silent: true });
  server.close();
}
