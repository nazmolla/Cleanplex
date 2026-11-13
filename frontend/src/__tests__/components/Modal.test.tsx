import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Modal from '../../components/Modal'

function Harness({ dismissible = true, onClose = () => {} }: { dismissible?: boolean; onClose?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      {open && (
        <Modal
          title="Delete segments?"
          dismissible={dismissible}
          onClose={() => { onClose(); setOpen(false) }}
          footer={<><button>Cancel</button><button>Delete</button></>}
        >
          <p>Body</p>
        </Modal>
      )}
    </>
  )
}

describe('Modal (#205)', () => {
  it('is a labelled dialog and moves focus inside when it opens', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByText('Open'))

    const dialog = screen.getByRole('dialog', { name: 'Delete segments?' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // The first action, not the close button, so Enter does not dismiss by accident.
    expect(screen.getByText('Cancel')).toHaveFocus()
  })

  it('starts on the control marked data-autofocus when there is one', () => {
    render(
      <Modal title="Delete?" onClose={() => {}}>
        <input aria-label="Don't ask again" type="checkbox" />
        <button data-autofocus>Cancel</button>
      </Modal>,
    )
    expect(screen.getByText('Cancel')).toHaveFocus()
  })

  it('keeps Tab and Shift+Tab inside the dialog', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByText('Open'))

    await userEvent.tab() // Cancel -> Delete
    expect(screen.getByText('Delete')).toHaveFocus()
    await userEvent.tab() // wraps to the first control, the close button
    expect(screen.getByLabelText('Close')).toHaveFocus()
    await userEvent.tab({ shift: true }) // and back to the last
    expect(screen.getByText('Delete')).toHaveFocus()
  })

  it('closes on Escape and returns focus to the control that opened it', async () => {
    render(<Harness />)
    const opener = screen.getByText('Open')
    await userEvent.click(opener)

    await userEvent.keyboard('{Escape}')

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it('closes on a backdrop press but not on a press inside the panel', async () => {
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)
    await userEvent.click(screen.getByText('Open'))

    fireEvent.mouseDown(screen.getByText('Body'))
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.mouseDown(screen.getByRole('dialog').parentElement!)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('ignores Escape and the backdrop while it is not dismissible', async () => {
    const onClose = vi.fn()
    render(<Harness dismissible={false} onClose={onClose} />)
    await userEvent.click(screen.getByText('Open'))

    await userEvent.keyboard('{Escape}')
    fireEvent.mouseDown(screen.getByRole('dialog').parentElement!)

    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('locks page scroll while open and restores it on close', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByText('Open'))
    expect(document.body.style.overflow).toBe('hidden')

    await userEvent.keyboard('{Escape}')
    expect(document.body.style.overflow).toBe('')
  })
})
