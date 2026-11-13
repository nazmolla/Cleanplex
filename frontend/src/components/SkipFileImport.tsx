import { useRef, useState } from 'react'
import { api } from '../api/client'
import { Upload, ClipboardPaste, Download, AlertTriangle, Check } from 'lucide-react'

interface ImportResult {
  imported: number
  source: string
  title: string
  warning: string | null
}

interface Props {
  plexGuid: string
  onImported?: (count: number) => void
}

type Mode = 'file' | 'paste'

export default function SkipFileImport({ plexGuid, onImported }: Props) {
  const [mode, setMode] = useState<Mode>('file')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ImportResult | null>(null)
  const [error, setError] = useState('')
  const [pasted, setPasted] = useState('')
  const [replace, setReplace] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const finish = (res: ImportResult) => {
    setResult(res)
    onImported?.(res.imported)
  }

  const importFile = async (file: File) => {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const form = new FormData()
      form.append('plex_guid', plexGuid)
      form.append('file', file)
      form.append('replace', String(replace))
      finish(await api.upload<ImportResult>('/api/segments/import', form))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed.')
    } finally {
      setBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  const importPaste = async () => {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      finish(
        await api.post<ImportResult>('/api/segments/import/paste', {
          plex_guid: plexGuid,
          text: pasted,
          replace,
        }),
      )
      setPasted('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed.')
    } finally {
      setBusy(false)
    }
  }

  const exportAs = async (fmt: 'edl' | 'mcf') => {
    const res = await api.get<{ body: string }>(
      `/api/segments/export/${encodeURIComponent(plexGuid)}?fmt=${fmt}`,
    )
    // Shown rather than downloaded: the viewer sandbox blocks page-initiated
    // downloads, so copying from a textarea is the reliable path.
    setPasted(res.body)
    setMode('paste')
  }

  return (
    <div className="bg-plex-card border border-plex-border rounded-xl p-4 space-y-3">
      <div className="flex items-center gap-2">
        <button
          onClick={() => setMode('file')}
          className={`px-2.5 py-1 rounded text-xs flex items-center gap-1 border ${
            mode === 'file'
              ? 'bg-plex-orange border-plex-orange text-black'
              : 'border-plex-border text-gray-400'
          }`}
        >
          <Upload size={12} /> Skip file
        </button>
        <button
          onClick={() => setMode('paste')}
          className={`px-2.5 py-1 rounded text-xs flex items-center gap-1 border ${
            mode === 'paste'
              ? 'bg-plex-orange border-plex-orange text-black'
              : 'border-plex-border text-gray-400'
          }`}
        >
          <ClipboardPaste size={12} /> Paste list
        </button>

        <label className="ml-auto flex items-center gap-1.5 text-xs text-muted">
          <input
            type="checkbox"
            checked={replace}
            onChange={e => setReplace(e.target.checked)}
            className="accent-plex-orange"
          />
          Replace existing
        </label>
      </div>

      {mode === 'file' ? (
        <div>
          <input
            ref={fileInput}
            type="file"
            accept=".skp,.edl,.mcf,.txt"
            disabled={busy}
            onChange={e => {
              const file = e.target.files?.[0]
              if (file) importFile(file)
            }}
            className="block w-full text-xs text-gray-400 file:mr-3 file:py-1.5 file:px-3 file:rounded file:border-0 file:text-xs file:bg-plex-border file:text-gray-200 hover:file:bg-plex-border/70"
          />
          <p className="text-[11px] text-muted mt-2">
            VideoSkip <code>.skp</code>, Kodi <code>.edl</code>, MovieContentFilter{' '}
            <code>.mcf</code>, or a plain <code>.txt</code> list. Importing skips the scan entirely.
          </p>
        </div>
      ) : (
        <div>
          <textarea
            value={pasted}
            onChange={e => setPasted(e.target.value)}
            rows={5}
            placeholder={'00:12:30 - 00:13:05 nudity\n1:02:11 to 1:02:20 violence'}
            className="w-full bg-plex-bg border border-plex-border rounded p-2 text-xs text-gray-200 font-mono"
          />
          <button
            onClick={importPaste}
            disabled={busy || !pasted.trim()}
            className="mt-2 px-3 py-1.5 rounded text-xs bg-plex-orange text-black font-medium disabled:opacity-40"
          >
            {busy ? 'Importing...' : 'Import list'}
          </button>
        </div>
      )}

      <div className="flex items-center gap-2 pt-1 border-t border-plex-border">
        <span className="text-[11px] text-muted">Export:</span>
        <button onClick={() => exportAs('edl')} className="text-[11px] text-gray-400 hover:text-gray-200 flex items-center gap-1">
          <Download size={11} /> EDL
        </button>
        <button onClick={() => exportAs('mcf')} className="text-[11px] text-gray-400 hover:text-gray-200 flex items-center gap-1">
          <Download size={11} /> MCF
        </button>
      </div>

      {error && (
        <p className="text-xs text-red-400 flex items-start gap-1.5">
          <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" /> {error}
        </p>
      )}

      {result && (
        <div className="text-xs space-y-1">
          <p className="text-green-400 flex items-center gap-1.5">
            <Check size={12} /> Imported {result.imported} segment
            {result.imported === 1 ? '' : 's'} from {result.source}.
          </p>
          {result.warning && (
            <p className="text-amber-400 flex items-start gap-1.5">
              <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" /> {result.warning}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
