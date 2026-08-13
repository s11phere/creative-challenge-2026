import { expect, test } from '@playwright/test'

test.describe('指令面板与键盘', () => {
  test('斜杠打开命令面板并可键盘选择指令', async ({ page }) => {
    await page.goto('/#qa')
    const composer = page.getByRole('combobox', { name: '消息' })
    await composer.press('/')
    const listbox = page.getByRole('listbox', { name: '指令与技能' })
    await expect(listbox).toBeVisible()
    await expect(page.getByRole('region', { name: '指令' })).toBeVisible()
    await expect(page.getByRole('region', { name: '技能' })).toBeVisible()
    await expect(listbox.locator('[role="option"]').first()).toBeVisible()
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    await expect(page.getByLabel(/已选择(指令|技能) /)).toBeVisible()
    await expect(listbox).toBeHidden()
  })

  test('Enter 提交普通文本消息', async ({ page }) => {
    await page.goto('/#qa')
    const composer = page.getByRole('combobox', { name: '消息' })
    await composer.fill('你好')
    const responsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v2/conversations/') &&
        response.url().endsWith('/turns'),
    )
    await page.keyboard.press('Enter')
    const response = await responsePromise
    expect(response.status()).toBe(202)
    await expect(composer).toHaveValue('')
  })

  test('空 composer 按 Enter 不提交', async ({ page }) => {
    await page.goto('/#qa')
    let turnPosts = 0
    page.on('request', (request) => {
      if (
        request.method() === 'POST' &&
        /\/api\/v2\/conversations\/[^/]+\/turns$/.test(request.url())
      ) {
        turnPosts += 1
      }
    })
    const composer = page.getByRole('combobox', { name: '消息' })
    await composer.click()
    await page.keyboard.press('Enter')
    await page.waitForTimeout(300)
    expect(turnPosts).toBe(0)
    await expect(composer).toHaveValue('')
  })
})
