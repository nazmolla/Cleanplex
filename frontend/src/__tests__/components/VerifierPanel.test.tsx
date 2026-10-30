import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import VerifierPanel from '../../components/VerifierPanel'

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

    await waitFor(() => expect(screen.getByText('Train')).toBeTruthy())
    expect((screen.getByRole('combobox') as HTMLSelectElement).disabled).toBe(true)
  })

  it('starts training and shows the model summary once trained', async () => {
    mockGet.mockResolvedValue({
      ...untrained,
      trained_at: 1_700_000_000,
      groups: { siglip: { auc: 0.81, n: 1456, false_positives: 764 } },
    })
    mockPost.mockResolvedValue({ ok: true })
    const onChange = vi.fn()
    render(<VerifierPanel value="10" onChange={onChange} />)

    await waitFor(() => expect(screen.getByText(/accuracy 0.81/)).toBeTruthy())
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '5' } })
    expect(onChange).toHaveBeenCalledWith('5')

    fireEvent.click(screen.getByText('Retrain'))
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/api/scan/verifier/train'))
  })
})
