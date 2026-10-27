// 桌宠窗「换身材」的几何计算 —— 纯函数，不 require electron，可单测。
// 调用方：pet.cjs 的 setPetBig()；测试：tools/test-pet-geometry.cjs。
//
// 为什么值得单独一个文件：这段逻辑只在真 Electron 里生效，而真 Electron 起不来
// （沙箱里没有 GUI），所以它一旦算错，唯一的表现就是「点了怎么回，桌宠窜到屏幕外」——
// 用户看到的是个 bug，代码里却一行报错都没有。锚点这种东西必须能被断言。

/** 桌宠本来的家。 */
const SMALL = { width: 220, height: 480 };
/** 「怎么回」工作台。两栏：左谁在说、右三条候选，底下还留着草稿与发送。 */
const BIG = { width: 440, height: 640 };

/**
 * 把一个 rect 夹进工作区。
 *
 * ⚠️ 夹的顺序要紧：先缩尺寸再夹原点 —— 否则窗口比工作区还大时，x 的合法区间是空的
 * （min > max），Math.min/max 会给出一个负坐标，窗口跑到屏幕外的负方位。
 */
function clampToWorkArea(rect, workArea) {
  const width = Math.min(rect.width, workArea.width);
  const height = Math.min(rect.height, workArea.height);
  const x = Math.min(Math.max(rect.x, workArea.x), workArea.x + workArea.width - width);
  const y = Math.min(Math.max(rect.y, workArea.y), workArea.y + workArea.height - height);
  return { x, y, width, height };
}

/**
 * 换身材后的 rect。**锚定右下角** —— 宽高各长出来的部分向左、向上伸展。
 *
 * @param {{x:number,y:number,width:number,height:number}} cur 当前 rect
 * @param {"big"|"small"} want 目标身材
 * @param {object} [opts]
 * @param {object|null} [opts.saved] 放大前存下的 rect（缩回时优先用它，因为用户可能拖过）
 * @param {{x:number,y:number,width:number,height:number}} [opts.workArea] 不传就不夹
 * @returns {{x:number,y:number,width:number,height:number}}
 */
function nextBounds(cur, want, opts) {
  const o = opts || {};
  let out;
  if (want === "big") {
    out = {
      x: cur.x - (BIG.width - cur.width),
      y: cur.y - (BIG.height - cur.height),
      width: BIG.width, height: BIG.height,
    };
  } else if (o.saved) {
    // 有存档就用存档的原点，但尺寸取标准小身材：存档是「放大前那一瞬间」的 rect，
    // 它一定是小身材的宽高；这里不拿 width 当判据（早先写成 `saved.width === BIG.width`
    // 是反的 —— 那会把唯一该接受的情况挡掉，缩回时按公式反推，桌宠真的会跳走）。
    out = { x: o.saved.x, y: o.saved.y, width: SMALL.width, height: SMALL.height };
  } else {
    // 没有存档（窗口重建过）：仍按「右下角不动」反推，绝不回退到屏幕默认位置。
    out = {
      x: cur.x + (BIG.width - SMALL.width),
      y: cur.y + (BIG.height - SMALL.height),
      width: SMALL.width, height: SMALL.height,
    };
  }
  if (o.workArea) {
    // 只在**放大**时夹：缩回原尺寸是回到用户自己放过的地方，不该被挪。
    return want === "big" ? clampToWorkArea(out, o.workArea) : out;
  }
  return out;
}

module.exports = { SMALL, BIG, nextBounds, clampToWorkArea };
