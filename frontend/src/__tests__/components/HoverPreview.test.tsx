import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import HoverPreview from '../../components/HoverPreview'

describe('HoverPreview (#114)', () => {
  it('plays the preview only while hovered', () => {
    render(<HoverPreview src="/api/segments/7/hover"><img alt="Flagged frame" /></HoverPreview>)
    const box = screen.getByAltText('Flagged frame').parentElement!
    expect(screen.queryByLabelText('Segment preview')).not.toBeInTheDocument()

    fireEvent.mouseEnter(box)
    const video = screen.getByLabelText('Segment preview') as HTMLVideoElement
    expect(video.getAttribute('src')).toBe('/api/segments/7/hover')
    expect(video.muted).toBe(true)
    expect(video.loop).toBe(true)

    fireEvent.mouseLeave(box)
    expect(screen.queryByLabelText('Segment preview')).not.toBeInTheDocument()
  })

  it('keeps the still when the preview is not rendered yet', () => {
    render(<HoverPreview src=""><img alt="Flagged frame" /></HoverPreview>)
    fireEvent.mouseEnter(screen.getByAltText('Flagged frame').parentElement!)
    expect(screen.queryByLabelText('Segment preview')).not.toBeInTheDocument()
  })
})
