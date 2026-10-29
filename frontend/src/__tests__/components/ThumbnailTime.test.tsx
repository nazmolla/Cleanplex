import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ThumbnailTime, { formatFrameTime } from '../../components/ThumbnailTime'

describe('ThumbnailTime (#133)', () => {
  it.each([
    [0, '0:00:00'],
    [59_999, '0:00:59'],
    [61_000, '0:01:01'],
    [3_723_000, '1:02:03'],
  ])('formats %d ms as %s', (ms, text) => {
    expect(formatFrameTime(ms)).toBe(text)
  })

  it('names the frame time on the thumbnail', () => {
    render(<ThumbnailTime ms={2_892_000} />)
    expect(screen.getByText('frame 0:48:12')).toBeInTheDocument()
  })

  it('shows nothing for segments scanned before the time was recorded', () => {
    const { container } = render(<ThumbnailTime ms={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
