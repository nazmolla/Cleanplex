import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ActorPicker } from '../../components/browse/ActorPicker'

const suggestions = [
  { name: 'Zoe Zed', titles: 3, segments: 1 },
  { name: 'Adam Ant', titles: 2, segments: 9 },
]

function setup(selected: string[] = []) {
  const props = { suggestions, sort: 'titles' as const, onSort: vi.fn(), selected, onChange: vi.fn(), match: 'any' as const, onMatch: vi.fn(), photos: {} }
  render(<ActorPicker {...props} />)
  fireEvent.click(screen.getByRole('button', { name: /^Filter by actor/ }))
  return props
}

describe('ActorPicker (#227)', () => {
  it('adds an actor to the selection and falls back to initials without a photo', () => {
    const p = setup(['Zoe Zed'])
    expect(screen.getByText('AA')).toBeTruthy()
    fireEvent.click(screen.getByRole('option', { name: /Adam Ant/ }).querySelector('button')!)
    expect(p.onChange).toHaveBeenCalledWith(['Zoe Zed', 'Adam Ant'])
  })

  it('offers the sort orders and, with two picked, any or all of them', () => {
    const p = setup(['Zoe Zed', 'Adam Ant'])
    fireEvent.change(screen.getByLabelText('Sort actors'), { target: { value: 'segments' } })
    expect(p.onSort).toHaveBeenCalledWith('segments')
    fireEvent.click(screen.getByRole('button', { name: 'All of them' }))
    expect(p.onMatch).toHaveBeenCalledWith('all')
    fireEvent.click(screen.getByRole('button', { name: /Clear/ }))
    expect(p.onChange).toHaveBeenCalledWith([])
  })

  it('closes on Escape', () => {
    setup()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})
