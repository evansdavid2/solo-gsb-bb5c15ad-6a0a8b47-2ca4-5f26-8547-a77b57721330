import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import ConflictCard from '../components/ConflictCard.jsx'
import { downloadJson, readFileText } from '../utils.js'

async function readDocFile(file) {
  const text = await readFileText(file)
  let data
  try {
    data = JSON.parse(text)
  } catch {
    // plain text transcript -> send to backend importer
    const doc = await api.importTranscript({ text })
    return { doc, label: file.name }
  }
  if (data && typeof data === 'object' && data.document && data.document.segments) {
    return { doc: data.document, label: file.name, meta: data }
  }
  if (data && Array.isArray(data.segments)) {
    return { doc: data, label: file.name }
  }
  throw new Error(`${file.name} 不是有效的校注文档（缺少 segments）`)
}

const STAT_LABELS = {
  segments: '转写片段',
  annotations: '校注',
}

const SIDE_LABELS = { base: '基准（Base）', a: '副本 A', b: '副本 B' }
const COLLECTION_LABELS = {
  segments: 'segments（片段）',
  annotations: 'annotations（校注）',
}

export default function MergePage() {
  const [projects, setProjects] = useState([])
  const [baseProjectId, setBaseProjectId] = useState('')
  const [baseDoc, setBaseDoc] = useState(null)
  const [baseLabel, setBaseLabel] = useState('')
  const [a, setA] = useState(null)
  const [b, setB] = useState(null)
  const [result, setResult] = useState(null)
  const [resolutions, setResolutions] = useState({})
  const [saveName, setSaveName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [savedLabel, setSavedLabel] = useState('')

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {})
  }, [])

  const describeRejection = (e) => {
    const d = e?.detail
    if (e?.status !== 409 || !d || typeof d !== 'object') return null
    const lines = []
    if (d.unresolved?.length) {
      lines.push(`未裁决冲突 ${d.unresolved.length} 项：${d.unresolved.join('、')}`)
    }
    if (d.errors?.length) {
      lines.push(`无效裁决 ${d.errors.length} 项：` +
        d.errors.map((x) => `${x.conflictId}（${x.message}）`).join('；'))
    }
    return lines.length
      ? `${d.message || '存在未裁决冲突或无效裁决，未保存'}\n${lines.join('\n')}`
      : null
  }

  // 422: an input document has duplicate / missing record ids.  Render
  // every problem (side, collection, row, id) so the user can fix the
  // file; the selected files stay in place and a corrected file can be
  // picked and previewed again right away.
  const describeInvalidInput = (e) => {
    const d = e?.detail
    if (e?.status !== 422 || !d || typeof d !== 'object'
        || !Array.isArray(d.problems)) return null
    const lines = d.problems.map((p) => {
      const rows = `第 ${(p.indexes || []).map((i) => i + 1).join('、')} 条`
      const where = `${SIDE_LABELS[p.side] || p.side} 的 ` +
        `${COLLECTION_LABELS[p.collection] || p.collection}${rows}`
      if (p.reason === 'duplicate') return `${where}：ID「${p.id}」重复`
      if (p.reason === 'invalid') {
        return `${where}：ID 必须是非空字符串（实际为 ${JSON.stringify(p.id)}）`
      }
      return `${where}：记录 ID 缺失或为空`
    })
    return ['输入文档存在无效的记录 ID，未执行合并（不会生成合并结果）；' +
      '请修正对应文件后重新选择并再次预览：', ...lines].join('\n')
  }

  const pickBaseProject = async (pid) => {
    setBaseProjectId(pid)
    setResult(null)
    if (!pid) { setBaseDoc(null); setBaseLabel(''); return }
    const p = projects.find((x) => x.id === pid)
    if (p) {
      setBaseDoc(p.document)
      setBaseLabel(`项目：${p.name}`)
    } else {
      const fresh = await api.getProject(pid)
      setBaseDoc(fresh.document)
      setBaseLabel(`项目：${fresh.name}`)
    }
  }

  const loadFile = async (file, side) => {
    setError('')
    try {
      const { doc, label } = await readDocFile(file)
      if (side === 'base') {
        setBaseDoc(doc); setBaseLabel(`文件：${label}`)
        setBaseProjectId('')
      } else if (side === 'a') {
        setA({ doc, label })
      } else {
        setB({ doc, label })
      }
      setResult(null)
    } catch (e) {
      setError(e.message)
    }
  }

  const canMerge = baseDoc && a?.doc && b?.doc

  const runMerge = async (extraResolutions = undefined, persist = false) => {
    if (!canMerge) return
    setBusy(true); setError(''); setSavedLabel('')
    try {
      const payload = {
        base: baseDoc,
        a: a.doc,
        b: b.doc,
        resolutions: extraResolutions === undefined ? resolutions : extraResolutions,
      }
      // only the explicit "save as" button persists; analysis / live
      // re-adjudication must not create projects as a side effect.
      if (persist && saveName.trim()) payload.saveAsProject = saveName.trim()
      const r = await api.merge(payload)
      setResult(r)
      if (r.savedProject) {
        setSaveName('')
        setSavedLabel(`已另存为新项目「${r.savedProject.name}」`)
        api.listProjects().then(setProjects)
      }
      return r
    } catch (e) {
      const invalidInput = describeInvalidInput(e)
      if (invalidInput) {
        // 422: the inputs themselves are invalid — the server produced no
        // merge result, so drop any stale preview (nothing to export or
        // write back).  The selected files are kept for replacement.
        setResult(null)
        setError(invalidInput)
      } else if (e.status === 409 && e.detail && 'unresolved' in e.detail) {
        // Save was refused: keep the latest preview (conflicts + errors stay
        // visible) so the user can fix the adjudication and retry.
        setError(describeRejection(e))
      } else {
        setError(e.message)
      }
    } finally {
      setBusy(false)
    }
  }

  const applyResolutions = async (next) => {
    setResolutions(next)
    await runMerge(next)
  }

  const setResolution = (conflictId, res) => {
    applyResolutions({ ...resolutions, [conflictId]: res })
  }

  const bulkChoose = (choice) => {
    const next = { ...resolutions }
    for (const c of result.conflicts) {
      if (!next[c.id]) next[c.id] = { choice }
    }
    applyResolutions(next)
  }

  // The server is authoritative: a conflict with a present resolution that
  // fails validation is neither "resolved" nor savable (it shows in
  // result.errors), so mere presence in `resolutions` is not enough.
  const invalidIds = useMemo(
    () => new Set((result?.errors || []).map((e) => e.conflictId)),
    [result],
  )
  const resolvedCount = result
    ? result.conflicts.filter((c) => resolutions[c.id] && !invalidIds.has(c.id)).length
    : 0
  const allResolved = !!result
    && result.unresolved.length === 0
    && result.errors.length === 0

  const exportResult = () => {
    if (!result) return
    const payload = {
      _mergedResult: true,
      mergedAt: new Date().toISOString(),
      base: baseLabel,
      a: a.label,
      b: b.label,
      unresolvedConflicts: result.unresolved,
      document: result.document,
    }
    downloadJson('merged-annotations.json', payload)
  }

  const writeBack = async () => {
    if (!baseProjectId || !allResolved) return
    setBusy(true); setError(''); setSavedLabel('')
    try {
      const r = await api.merge({
        projectId: baseProjectId,
        a: a.doc,
        b: b.doc,
        resolutions,
      })
      setResult(r)
      setError('')
      // report only what the server actually persisted
      if (r.savedProject) {
        setSavedLabel(`已写回基准项目「${r.savedProject.name}」`)
      }
    } catch (e) {
      // 409 with unresolved/errors: refuse to write back; the base document
      // stays unchanged. The live preview already lists the same problems.
      // 422: the (re-read) base document or a copy has invalid record ids —
      // no merge result exists, so drop the stale preview as well.
      const invalidInput = describeInvalidInput(e)
      if (invalidInput) setResult(null)
      setError(invalidInput || describeRejection(e) || e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page merge-page">
      <div className="page-head">
        <h2>三方合并（离线副本 A / B 对共同基准）</h2>
      </div>
      {error && (
        <div className="error" style={{ whiteSpace: 'pre-line' }}>{error}</div>
      )}

      <div className="merge-sources">
        <SourceCard
          title="① 共同基准（Base）"
          badge={baseLabel}
        >
          <select
            value={baseProjectId}
            onChange={(e) => pickBaseProject(e.target.value)}
          >
            <option value="">— 选择一个项目作为基准 —</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
          <FilePick onFile={(f) => loadFile(f, 'base')} />
        </SourceCard>
        <SourceCard title="② 离线副本 A" badge={a?.label}>
          <FilePick onFile={(f) => loadFile(f, 'a')} />
          {a && <DocSummary doc={a.doc} />}
        </SourceCard>
        <SourceCard title="③ 离线副本 B" badge={b?.label}>
          <FilePick onFile={(f) => loadFile(f, 'b')} />
          {b && <DocSummary doc={b.doc} />}
        </SourceCard>
      </div>

      <div className="merge-run">
        <button
          className="btn primary big"
          disabled={!canMerge || busy}
          onClick={() => { setResolutions({}); runMerge({}) }}
        >
          {busy ? '合并中…' : '开始三方合并'}
        </button>
        {!canMerge && (
          <span className="muted small">
            需要先指定基准以及 A、B 两份文档（项目或导出的离线副本 JSON /
            纯文本转写稿）
          </span>
        )}
      </div>

      {result && (
        <MergeResult
          result={result}
          resolutions={resolutions}
          invalidIds={invalidIds}
          resolvedCount={resolvedCount}
          allResolved={!!allResolved}
          setResolution={setResolution}
          exportResult={exportResult}
          saveName={saveName}
          setSaveName={setSaveName}
          runMerge={(persist) => runMerge(undefined, persist)}
          bulkChoose={bulkChoose}
          canWriteBack={!!baseProjectId}
          writeBack={writeBack}
          busy={busy}
          savedLabel={savedLabel}
        />
      )}
    </div>
  )
}

function SourceCard({ title, badge, children }) {
  return (
    <div className="card source-card">
      <h4>{title}</h4>
      {badge && <div className="muted small source-badge">{badge}</div>}
      {children}
    </div>
  )
}

function FilePick({ onFile }) {
  return (
    <label className="btn small file-pick">
      选择文件…
      <input
        type="file" hidden
        accept=".json,.txt,.srt,.vtt"
        onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
      />
    </label>
  )
}

function DocSummary({ doc }) {
  return (
    <div className="muted small">
      ✓ {doc.segments.length} 片段 · {doc.annotations.length} 校注
    </div>
  )
}

function MergeResult({
  result, resolutions, invalidIds, resolvedCount, allResolved, setResolution,
  exportResult, saveName, setSaveName, runMerge, bulkChoose,
  canWriteBack, writeBack, busy, savedLabel,
}) {
  const { stats, conflicts, changes, document: merged } = result
  const unresolvedIds = new Set(result.unresolved || [])
  const pendingCount = conflicts.filter(
    (c) => !resolutions[c.id] || unresolvedIds.has(c.id) || invalidIds.has(c.id),
  ).length

  const autoGroups = useMemo(() => {
    const groups = {}
    for (const ch of changes) {
      if (ch.action === 'unchanged' || ch.action === 'conflict') continue
      const key = `${ch.kind}.${ch.action}`
      ;(groups[key] ||= []).push(ch)
    }
    return groups
  }, [changes])

  return (
    <div className="merge-result">
      <div className="panel stats-panel">
        <h3>合并结果概览</h3>
        <div className="stats-grid">
          {['segments', 'annotations'].map((kind) => (
            <div key={kind} className="stat-box">
              <strong>{STAT_LABELS[kind]}</strong>
              <span>新增 {stats[kind].added}</span>
              <span>修改 {stats[kind].modified}</span>
              <span>删除 {stats[kind].deleted}</span>
            </div>
          ))}
          <div className={`stat-box ${conflicts.length ? 'has-conflict' : ''}`}>
            <strong>冲突</strong>
            <span>{conflicts.length} 项</span>
            <span>已裁决 {resolvedCount}</span>
            {result.errors?.length > 0 && <span>裁决有误 {result.errors.length}</span>}
            <span>
              {allResolved
                ? '✓ 全部解决'
                : `待处理 ${pendingCount}`}
            </span>
          </div>
        </div>
      </div>

      <details className="panel auto-changes" open={conflicts.length === 0}>
        <summary>
          自动合并的互不重叠改动（
          {Object.values(autoGroups).reduce((n, g) => n + g.length, 0)} 项）
        </summary>
        <div className="change-groups">
          {Object.entries(autoGroups).map(([key, items]) => (
            <div key={key} className="change-group">
              <strong>{changeLabel(key)}（{items.length}）</strong>
              <div className="change-items">
                {items.slice(0, 30).map((ch) => (
                  <span key={`${ch.kind}-${ch.itemId}-${ch.action}`} className="tag">
                    {ch.kind === 'segments' ? '片段' : '校注'} {shortId(ch.itemId)}
                    {ch.side ? ` · 来自${ch.side.toUpperCase()}` : ''}
                  </span>
                ))}
                {items.length > 30 && <span className="muted">…</span>}
              </div>
            </div>
          ))}
          {Object.keys(autoGroups).length === 0 && (
            <span className="muted small">无自动改动</span>
          )}
        </div>
      </details>

      {conflicts.length > 0 && (
        <div className="conflicts-section">
          <h3>
            需要逐项裁决的冲突（{resolvedCount}/{conflicts.length}
            {result.errors?.length > 0 ? `，${result.errors.length} 项裁决无效` : ''}）
          </h3>
          <div className="conflict-bulk">
            <button className="btn small" onClick={() => bulkChoose('a')}>
              未裁决项全部取 A
            </button>
            <button className="btn small" onClick={() => bulkChoose('b')}>
              未裁决项全部取 B
            </button>
          </div>
          {conflicts.map((c, i) => {
            const err = result.errors?.find((e) => e.conflictId === c.id)
            return (
              <ConflictCard
                key={c.id}
                conflict={c}
                index={i}
                value={resolutions[c.id] || null}
                invalid={invalidIds.has(c.id)}
                errorMessage={err?.message || ''}
                onChange={(res) => setResolution(c.id, res)}
              />
            )
          })}
        </div>
      )}

      {result.errors?.length > 0 && (
        <div className="error">
          以下裁决无效（冲突条目暂未进入合并文档，修正后可重新预览与保存）：
          <ul>
            {result.errors.map((e) => (
              <li key={e.conflictId}>{e.conflictId}: {e.message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="panel final-doc">
        <h3>
          合并后的文档（{merged.segments.length} 片段 ·{' '}
          {merged.annotations.length} 校注）
        </h3>
        {!allResolved && (
          <div className="warn-box">
            {result.unresolved.length > 0 && (
              <div>
                尚有 {result.unresolved.length} 项冲突未裁决；这些条目当前不会
                出现在合并文档中。
              </div>
            )}
            {result.errors?.length > 0 && (
              <div>
                另有 {result.errors.length} 项裁决无效，对应条目也不会出现在
                合并文档中；请修正后重新预览。
              </div>
            )}
            <div>存在未裁决或无效裁决时无法保存（另存 / 写回均会被服务端拒绝）。</div>
          </div>
        )}
        <div className="final-actions">
          <button className="btn" onClick={exportResult}>
            导出裁决结果 JSON
          </button>
          <div className="save-as">
            <input
              placeholder="另存为新项目名称"
              value={saveName}
              onChange={(e) => setSaveName(e.target.value)}
            />
            <button
              className="btn primary"
              disabled={!allResolved || !saveName.trim() || busy}
              title={!allResolved ? '需要全部冲突裁决有效后才能另存' : ''}
              onClick={() => runMerge(true)}
            >
              全部裁决有效后另存为新项目
            </button>
          </div>
          {canWriteBack && (
            <button
              className="btn"
              disabled={!allResolved || busy}
              title={
                allResolved
                  ? '以合并结果覆盖基准项目的当前文档'
                  : '需要全部冲突裁决有效后才能写回'
              }
              onClick={writeBack}
            >
              全部裁决有效后写回基准项目
            </button>
          )}
        </div>
        {result.savedProject && (
          <div className="ok">
            {savedLabel || '已保存为项目'}：
            <Link to={`/projects/${result.savedProject.id}`}>
              {result.savedProject.name}
            </Link>
          </div>
        )}
        <details>
          <summary>预览合并文档 JSON</summary>
          <pre className="json-preview">
            {JSON.stringify(merged, null, 2)}
          </pre>
        </details>
      </div>
    </div>
  )
}

function changeLabel(key) {
  const [kind, action] = key.split('.')
  const k = kind === 'segments' ? '片段' : '校注'
  const a = { added: '新增', modified: '修改', deleted: '删除' }[action]
  return `${k}${a}`
}

function shortId(id) {
  if (!id) return ''
  return id.length > 16 ? id.slice(-8) : id
}
