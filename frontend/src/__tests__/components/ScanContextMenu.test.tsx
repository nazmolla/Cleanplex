import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ScanContextMenu from '../../components/ScanContextMenu'

const target = { x: 10, y: 10, label: 'Show – Season 1', guids: ['a', 'b'] }

describe('ScanContextMenu', () => {
  it('labels the target with its title count', () => {
    render(<ScanContextMenu target={target} onScan={vi.fn()} onClose={vi.fn()} />)

    expect(screen.getByText('Show – Season 1 · 2 titles')).toBeInTheDocument()
  })

  it('rescans now or tonight and then closes', () => {
    const onScan = vi.fn()
    const onClose = vi.fn()
    render(<ScanContextMenu target={target} onScan={onScan} onClose={onClose} />)

    fireEvent.click(screen.getByText('Rescan now'))
    fireEvent.click(screen.getByText('Rescan tonight'))

    expect(onScan.mock.calls).toEqual([[target, true], [target, false]])
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('closes on Escape and on an outside click', () => {
    const onClose = vi.fn()
    render(<ScanContextMenu target={target} onScan={vi.fn()} onClose={onClose} />)

    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.mouseDown(document.body)

    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
