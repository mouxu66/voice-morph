/**
 * 定向重打安装目录里的 app.asar：把源码里的**一个文件**换进包，其余原样保留。
 *
 * 用法：
 *   node scripts/repack_asar.cjs                       # 默认换 electron/pet/pet.html
 *   node scripts/repack_asar.cjs electron/main.cjs     # 换别的文件（相对 web/ 的路径）
 *   node scripts/repack_asar.cjs electron/pet/pet.html "0.2.6+ 说明"
 *
 * 为什么要走「全量 extract → 换一个文件 → pack」而不是 asar 的 patch：
 *   asar 是归档格式，没有原地改单文件的接口。历史上试过只 pack 一个子目录再拼，
 *   结果文件顺序/头信息不一致，asar 能列出内容但 Electron 读出来是坏的。
 *   47MB / 5310 条目，extract+pack 约 20 秒 —— 比推一个坏包便宜得多。
 *
 * 三道验证（少一道就可能推一个启动即崩的包）：
 *   ① 条目数必须与旧包一致（只换内容不增删）
 *   ② 换完后整目录 pack/unpack 一遍，逐文件 hash 必须与替换后的源码树相同
 *   ③ 新包里那个文件必须与源码逐字节相同
 *
 * 备份：每次在 app.asar 旁边留一个 .bak-<毫秒时间戳>（45MB）。**会堆积** ——
 * 项目里已经躺了 28 个（约 1.3GB），需要时手动清。
 */
const asar = require('@electron/asar')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const crypto = require('node:crypto')

const RES = path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'voice-morph-desktop', 'resources')
const ASAR = path.join(RES, 'app.asar')
const REL = (process.argv[2] || 'electron/pet/pet.html').split('\\').join('/')
const NOTE = process.argv[3] || ''
const SRC = path.join(__dirname, '..', REL)
const TMP = path.join(os.tmpdir(), 'asar-repack')

const sha = (p) => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex')

function walk(dir, base = dir, out = new Map()) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name)
    if (e.isDirectory()) walk(full, base, out)
    else out.set(path.relative(base, full).replace(/\\/g, '/'), full)
  }
  return out
}

;(async () => {
  if (!fs.existsSync(ASAR)) throw new Error('找不到 ' + ASAR)
  const backup = ASAR + '.bak-' + Date.now()
  fs.copyFileSync(ASAR, backup)
  console.log('备份 →', backup, (fs.statSync(backup).size / 1048576).toFixed(1) + ' MB')

  const before = asar.listPackage(ASAR, { isPack: false })
  console.log('旧包条目:', before.length)

  // extract 旧包
  const oldDir = path.join(TMP, 'old')
  fs.rmSync(TMP, { recursive: true, force: true })
  fs.mkdirSync(oldDir, { recursive: true })
  asar.extractAll(ASAR, oldDir)
  console.log('已解出旧包')

  // 换文件
  const target = path.join(oldDir, REL.split('/').join(path.sep))
  if (!fs.existsSync(target)) throw new Error('旧包里没有 ' + REL)
  const oldHash = sha(target)
  fs.copyFileSync(SRC, target)
  console.log(REL, oldHash.slice(0, 12), '→', sha(target).slice(0, 12))

  // 写入本次重打的标注（audit_asar_freshness 会把它打出来，人读用）
  const pkgFile = path.join(oldDir, 'package.json')
  if (fs.existsSync(pkgFile)) {
    const pkg = JSON.parse(fs.readFileSync(pkgFile, 'utf8'))
    pkg._asarVersion = '0.2.5+2026-10-23 建议面板重构（#recs 上下文推荐 + 输入框搜索 + ♪ 并入抽屉；base 0.2.4）'
    fs.writeFileSync(pkgFile, JSON.stringify(pkg, null, 2) + "\n", 'utf8')
    console.log('标注 →', pkg._asarVersion)
  }

  // pack
  const next = path.join(TMP, 'app.next.asar')
  // createPackage 是 async（@electron/asar 3.x）：不 await 就会在文件写完之前去 listPackage
  await asar.createPackage(oldDir, next)
  const after = asar.listPackage(next, { isPack: false })
  console.log('新包条目:', after.length)
  if (after.length !== before.length) throw new Error('条目数变了：' + before.length + ' → ' + after.length)

  // ② 新旧逐文件比对
  const newDir = path.join(TMP, 'new')
  fs.mkdirSync(newDir, { recursive: true })
  asar.extractAll(next, newDir)
  const a = walk(oldDir)
  const b = walk(newDir)
  if (a.size !== b.size) throw new Error('文件数变了：' + a.size + ' → ' + b.size)
  const diff = []
  for (const [k, fa] of a) {
    const fb = b.get(k)
    if (!fb) { diff.push('缺失 ' + k); continue }
    if (sha(fa) !== sha(fb)) diff.push('内容不同 ' + k)
  }
  if (diff.length) throw new Error('pack/unpack 不是无损的：' + diff.slice(0, 5).join(', '))
  console.log('✓ pack/unpack 无损（' + a.size + ' 个文件逐一比对）')

  // ③ 新包里那个文件必须与源码逐字节相同
  const packed = path.join(newDir, REL.split('/').join(path.sep))
  if (sha(packed) !== sha(SRC)) throw new Error('pack 后的 ' + REL + ' 与源码不一致')
  console.log('✓ 新包内 ' + REL + ' 与源码一致')

  // 替换
  fs.renameSync(next, ASAR)
  fs.rmSync(TMP, { recursive: true, force: true })
  console.log('✓ 已替换 app.asar', (fs.statSync(ASAR).size / 1048576).toFixed(1) + ' MB')
})().catch((e) => { console.error('FAILED:', e.message); process.exit(1) })
