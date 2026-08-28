import assert from "node:assert/strict";
import { access, readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";


const outputRoot = resolve(import.meta.dirname, "..", "runtime", "frontend");

test("Vite emits the static RotoWeave production shell", async () => {
  const html = await readFile(resolve(outputRoot, "index.html"), "utf8");
  assert.match(html, /<html lang="zh-CN">/i);
  assert.match(html, /RotoWeave 4\.0 客户端/);
  assert.match(html, /正在启动 RotoWeave 4\.0 客户端/);
  const script = html.match(/src="(\/assets\/[^"]+\.js)"/)?.[1];
  assert.ok(script, "production HTML must reference a hashed Vite JavaScript asset");
  await access(resolve(outputRoot, script.slice(1)));
  assert.doesNotMatch(html, /_next\/|vinext|Your site is taking shape|react-loading-skeleton/i);
});

test("production chunks mount only the 4.0 client shell and Hash navigation", async () => {
  const assetRoot = resolve(outputRoot, "assets");
  const files = await readdir(assetRoot);
  const chunks = await Promise.all(
    files
      .filter((file) => file.endsWith(".js"))
      .map((file) => readFile(resolve(assetRoot, file), "utf8")),
  );
  const javascript = chunks.join("\n");

  for (const label of [
    "CLIENT 4.0.0",
    "建立第一个角色",
    "全局设置",
    "素材库",
    "导出设置",
    "Workspace Format 3",
    "选择 Workspace Format 3 工作区",
    "新建工作区",
    "打开工作区",
    "最近工作区",
    "当前工作区",
    "退出工作区",
    "链接中的角色已不存在",
    "保存并继续",
    "放弃修改",
    "/characters/",
    "/materials",
    "/actions/",
  ]) {
    assert.ok(javascript.includes(label), `missing 4.0 shell contract: ${label}`);
  }
  for (const legacyLabel of [
    "批量统一纹理尺寸（清晰度）",
    "MATTE 3.0",
    "幕色橡皮擦",
  ]) {
    assert.ok(!javascript.includes(legacyLabel), `legacy workflow leaked into production entry: ${legacyLabel}`);
  }
});

test("matte reset action stays in the inspector content flow", async () => {
  const styles = await readFile(
    resolve(import.meta.dirname, "..", "app", "globals.css"),
    "utf8",
  );

  assert.match(styles, /\.matte-finish-actions\s*\{\s*margin-top:\s*0;/);
  assert.doesNotMatch(styles, /\.matte-finish-actions\s*\{[^}]*margin-top:\s*auto;/);
});

test("production chunks expose the 4.0 action editing workspace", async () => {
  const assetRoot = resolve(outputRoot, "assets");
  const files = await readdir(assetRoot);
  const javascript = (
    await Promise.all(
      files
        .filter((file) => file.endsWith(".js"))
        .map((file) => readFile(resolve(assetRoot, file), "utf8")),
    )
  ).join("\n");

  for (const label of [
    "4.0 动作编辑器",
    "素材帧库",
    "帧调整",
    "阴影调整",
    "归一 1/24 秒",
    "有未保存修改",
    "重置到上一次保存",
    "循环播放",
    "阴影启用方式",
    "继承全局（当前",
    "强制启用",
    "强制关闭",
    "当前动作全部继承全局",
  ]) {
    assert.ok(javascript.includes(label), `missing action editor label: ${label}`);
  }
});

test("production chunks expose the 4.0 material management workspace", async () => {
  const assetRoot = resolve(outputRoot, "assets");
  const files = await readdir(assetRoot);
  const javascript = (
    await Promise.all(
      files
        .filter((file) => file.endsWith(".js"))
        .map((file) => readFile(resolve(assetRoot, file), "utf8")),
    )
  ).join("\n");

  for (const label of [
    "4.0 素材管理",
    "Shift 连选 · Ctrl 增减选择",
    "单帧预览",
    "选择序列",
    "纯色幕优先",
    "保护主体颜色",
    "透明",
    "白底",
    "黑底",
    "适配",
    "确认抠图",
    "处理后",
    "特效层",
    "PS 拼图",
    "回导处理后",
  ]) {
    assert.ok(javascript.includes(label), `missing material manager label: ${label}`);
  }
});

test("production chunks expose calibrated units, visible guides, and selective delivery", async () => {
  const assetRoot = resolve(outputRoot, "assets");
  const files = await readdir(assetRoot);
  const javascript = (
    await Promise.all(
      files
        .filter((file) => file.endsWith(".js"))
        .map((file) => readFile(resolve(assetRoot, file), "utf8")),
    )
  ).join("\n");

  for (const label of [
    "跨角色尺寸预设",
    "尺寸矩形框预设",
    "全局辅助线显示",
    "尺寸框",
    "地平线",
    "阴影线",
    "Unity unit",
    "100 PPU",
    "重合时画布会以蓝/橙双轨显示",
    "适配",
    "参与导出",
    "保留在工作区，不参与本次交付。",
  ]) {
    assert.ok(javascript.includes(label), `missing calibration/delivery label: ${label}`);
  }
});
