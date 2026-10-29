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

    expect(onScan.mock.calls).toEqual([[target, true, false], [target, false, false]])
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('closes on Escape and on an outside click', () => {
    const onClose = vi.fn()
    render(<ScanContextMenu target={target} onScan={vi.fn()} onClose={onClose} />)

    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.mouseDown(document.body)

    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('offers only the listed titles when no show is attached', () => {
    render(<ScanContextMenu target={target} onScan={vi.fn()} onClose={vi.fn()} />)

    expect(screen.queryByText('Rescan whole show now')).not.toBeInTheDocument()
  })

  it('offers the whole show next to the listed episodes and says which is which (#137)', () => {
    const onScan = vi.fn()
    const show = { ...target, label: 'The Show', wholeShow: { showGuid: 'show-1', showTitle: 'The Show' } }
    render(<ScanContextMenu target={show} onScan={onScan} onClose={vi.fn()} />)

    expect(screen.getByText('Listed episodes (2)')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Rescan whole show tonight'))
    fireEvent.click(screen.getByText('Rescan now'))

    expect(onScan.mock.calls).toEqual([[show, false, true], [show, true, false]])
  })
})
