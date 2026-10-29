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


describe('RelabelButton label list shared across cards (#142)', () => {
  const fresh = async () => {
    vi.resetModules()
    const apiModule = await import('../../api/client')
    const Button = (await import('../../components/RelabelButton')).default
    return { get: apiModule.api.get as ReturnType<typeof vi.fn>, Button }
  }

  it('shows every label on a card mounted before another card filled the shared list', async () => {
    const { get, Button } = await fresh()
    get.mockResolvedValue({ labels: LABELS })
    render(<><Button seg={{ id: 1, labels: 'BUTTOCKS_EXPOSED' }} onChange={vi.fn()} />
              <Button seg={{ id: 2, labels: 'FEMALE_BREAST_EXPOSED' }} onChange={vi.fn()} /></>)

    fireEvent.click(screen.getAllByLabelText('Edit labels')[0])
    await screen.findByLabelText('buttocks exposed')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.click(screen.getAllByLabelText('Edit labels')[1])
    expect(await screen.findByLabelText('female breast exposed')).toBeChecked()
    expect(screen.getAllByRole('checkbox')).toHaveLength(LABELS.length)
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('makes one request when two dialogs open at once', async () => {
    const { get, Button } = await fresh()
    get.mockResolvedValue({ labels: LABELS })
    render(<><Button seg={{ id: 1, labels: 'BUTTOCKS_EXPOSED' }} onChange={vi.fn()} />
              <Button seg={{ id: 2, labels: 'BUTTOCKS_EXPOSED' }} onChange={vi.fn()} /></>)

    fireEvent.click(screen.getAllByLabelText('Edit labels')[0])
    await screen.findByLabelText('buttocks exposed')

    expect(get).toHaveBeenCalledTimes(1)
  })

  it('shows the error with a retry instead of an empty dialog, and a retry succeeds', async () => {
    const { get, Button } = await fresh()
    get.mockRejectedValueOnce(new Error('offline'))
    get.mockResolvedValue({ labels: LABELS })
    render(<Button seg={{ id: 1, labels: 'BUTTOCKS_EXPOSED' }} onChange={vi.fn()} />)

    fireEvent.click(screen.getByLabelText('Edit labels'))
    expect(await screen.findByText(/Could not load the label list/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByLabelText('buttocks exposed')).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('does not remember a failed load for the next open', async () => {
    const { get, Button } = await fresh()
    get.mockRejectedValueOnce(new Error('offline'))
    get.mockResolvedValue({ labels: LABELS })
    render(<Button seg={{ id: 1, labels: 'BUTTOCKS_EXPOSED' }} onChange={vi.fn()} />)

    fireEvent.click(screen.getByLabelText('Edit labels'))
    await screen.findByText(/Could not load the label list/)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByLabelText('Edit labels'))

    expect(await screen.findByLabelText('buttocks exposed')).toBeInTheDocument()
  })
})
