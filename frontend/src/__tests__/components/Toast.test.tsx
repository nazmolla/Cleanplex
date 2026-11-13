import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { Toaster, toast, clearToasts } from '../../components/Toast'

describe('Toast (#205)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    render(<Toaster />)
  })
  afterEach(() => {
    act(() => clearToasts())
    vi.useRealTimers()
  })

  it('announces an error in the alert region and keeps it until dismissed', () => {
    act(() => toast.error('Could not skip: 500'))
    expect(screen.getByRole('alert')).toHaveTextContent('Could not skip: 500')

    act(() => { vi.advanceTimersByTime(60_000) })
    expect(screen.getByText('Could not skip: 500')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByText('Could not skip: 500')).not.toBeInTheDocument()
  })

  it('announces a confirmation politely and removes it by itself', () => {
    act(() => toast.success('Jumped to 00:01:00'))
    expect(screen.getByRole('status')).toHaveTextContent('Jumped to 00:01:00')
    expect(screen.getByRole('alert')).toBeEmptyDOMElement()

    act(() => { vi.advanceTimersByTime(4000) })
    expect(screen.queryByText('Jumped to 00:01:00')).not.toBeInTheDocument()
  })

  it('shows at most four messages, dropping the oldest', () => {
    act(() => { for (let i = 1; i <= 5; i++) toast.error(`error ${i}`) })
    expect(screen.queryByText('error 1')).not.toBeInTheDocument()
    expect(screen.getByText('error 5')).toBeInTheDocument()
    expect(screen.getAllByLabelText('Dismiss')).toHaveLength(4)
  })
})
