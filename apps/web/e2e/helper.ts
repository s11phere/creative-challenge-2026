import { expect, type Page } from '@playwright/test'

/** Fill the composer (accessible name `消息`) and submit with Enter. */
export async function sendMessage(page: Page, text: string) {
  const composer = page.getByRole('combobox', { name: '消息' })
  await composer.fill(text)
  await composer.press('Enter')
}

/** Wait for the terminal assistant answer frame to contain *text*. */
export async function expectFinalAnswer(page: Page, text: string) {
  await expect(page.locator('.chat-final-answer')).toContainText(text)
}
