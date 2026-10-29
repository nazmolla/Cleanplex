import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import RelabelButton from '../../components/RelabelButton'

vi.mock('../../api/client', () => ({ api: { get: vi.fn(), put: vi.fn() } }))

import { api } from '../../api/client'
const mockGet = api.get as ReturnType<typeof vi.fn>
const mockPut = api.put as ReturnType<typeof vi.fn>

afterEach(() => vi.clearAllMocks())

const LABELS = ['FEMALE_BREAST_EXPOSED', 'BUTTOCKS_EXPOSED', 'MALE_GENITALIA_EXPOSED']

async function openEditor(labels = 'BUTTOCKS_EXPOSED') {
  mockGet.mockResolvedValue({ labels: LABELS })
  const onChange = vi.fn()
  render(<RelabelButton seg={{ id: 7, labels }} onChange={onChange} />)
  fireEvent.click(screen.getByLabelText('Edit labels'))
  await screen.findByLabelText('buttocks exposed')
  return onChange
}

describe('RelabelButton (#125)', () => {
  it('opens with the segment current labels ticked', async () => {
    await openEditor('BUTTOCKS_EXPOSED')
    expect(screen.getByLabelText('buttocks exposed')).toBeChecked()
    expect(screen.getByLabelText('female breast exposed')).not.toBeChecked()
  })

  it('saves the corrected labels and reports them', async () => {
    mockPut.mockResolvedValue({ labels: 'MALE_GENITALIA_EXPOSED' })
    const onChange = await openEditor()

    fireEvent.click(screen.getByLabelText('buttocks exposed'))
    fireEvent.click(screen.getByLabelText('male genitalia exposed'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(7, 'MALE_GENITALIA_EXPOSED'))
    expect(mockPut).toHaveBeenCalledWith('/api/segments/7/labels', { labels: ['MALE_GENITALIA_EXPOSED'] })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('cannot save with no label picked', async () => {
    await openEditor()
    fireEvent.click(screen.getByLabelText('buttocks exposed'))
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('keeps the dialog open and shows the error when saving fails', async () => {
    mockPut.mockRejectedValue(new Error('boom'))
    const onChange = await openEditor()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('boom')).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('cancelling sends nothing', async () => {
    await openEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mockPut).not.toHaveBeenCalled()
  })
})
