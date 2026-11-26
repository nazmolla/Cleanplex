import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import VerifierPanel, { VerifierTraining } from '../../components/VerifierPanel'

vi.mock('../../api/client', () => ({ api: { get: vi.fn(), post: vi.fn() } }))

import { api } from '../../api/client'
const mockGet = api.get as ReturnType<typeof vi.fn>
const mockPost = api.post as ReturnType<typeof vi.fn>

afterEach(() => vi.clearAllMocks())

const untrained = { running: false, progress: 0, error: '', trained_at: null, groups: {} }

describe('VerifierPanel (#165)', () => {
  it('keeps the strictness choice disabled until a model is trained', async () => {
    mockGet.mockResolvedValue(untrained)
    render(<VerifierPanel value="0" onChange={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/Train the filter under Maintenance/)).toBeTruthy())
    expect((screen.getByRole('combobox') as HTMLSelectElement).disabled).toBe(true)
  })

  it('lets a trained filter change strictness', async () => {
    mockGet.mockResolvedValue({ ...untrained, trained_at: 1_700_000_000 })
    const onChange = vi.fn()
    render(<VerifierPanel value="10" onChange={onChange} />)

    await waitFor(() => expect((screen.getByRole('combobox') as HTMLSelectElement).disabled).toBe(false))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '5' } })
    expect(onChange).toHaveBeenCalledWith('5')
  })
})

describe('VerifierTraining (#236)', () => {
  it('shows each model in a table and retrains', async () => {
    mockGet.mockResolvedValue({
      ...untrained,
      trained_at: 1_700_000_000,
      groups: { 'siglip:BUTTOCKS_EXPOSED': { auc: 0.81, n: 1456, false_positives: 764 } },
    })
    mockPost.mockResolvedValue({ ok: true })
    render(<VerifierTraining />)

    const row = await screen.findByRole('row', { name: /siglip:BUTTOCKS_EXPOSED/ })
    expect(row.textContent).toContain('764')
    expect(row.textContent).toContain('1456')
    expect(row.textContent).toContain('0.81')

    fireEvent.click(screen.getByText('Retrain'))
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/api/scan/verifier/train'))
  })
})
