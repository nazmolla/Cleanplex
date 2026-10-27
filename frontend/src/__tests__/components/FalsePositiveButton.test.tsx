import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import FalsePositiveButton from '../../components/FalsePositiveButton'

vi.mock('../../api/client', () => ({ api: { post: vi.fn() } }))

import { api } from '../../api/client'
const mockPost = api.post as ReturnType<typeof vi.fn>

afterEach(() => vi.clearAllMocks())

describe('FalsePositiveButton (#101)', () => {
  it('marks a segment and reports the change', async () => {
    mockPost.mockResolvedValue({ ok: true })
    const onChange = vi.fn()
    render(<FalsePositiveButton seg={{ id: 7 }} onChange={onChange} />)

    fireEvent.click(screen.getByLabelText('Mark as false positive'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(7, true))
    expect(mockPost).toHaveBeenCalledWith('/api/segments/7/false-positive', { false_positive: true })
  })

  it('undoes a mark', async () => {
    mockPost.mockResolvedValue({ ok: true })
    const onChange = vi.fn()
    render(<FalsePositiveButton seg={{ id: 7, false_positive: true }} onChange={onChange} />)

    fireEvent.click(screen.getByLabelText('Undo false positive'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(7, false))
    expect(mockPost).toHaveBeenCalledWith('/api/segments/7/false-positive', { false_positive: false })
  })

  it('leaves the list alone when the server refuses', async () => {
    mockPost.mockRejectedValue(new Error('500'))
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const onChange = vi.fn()
    render(<FalsePositiveButton seg={{ id: 7 }} onChange={onChange} />)

    fireEvent.click(screen.getByLabelText('Mark as false positive'))

    await waitFor(() => expect(alert).toHaveBeenCalled())
    expect(onChange).not.toHaveBeenCalled()
  })
})
