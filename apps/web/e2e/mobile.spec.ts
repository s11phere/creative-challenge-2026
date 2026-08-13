import { expect, test } from '@playwright/test'

// Runs only under the `mobile-chromium` project (390x844 viewport).
test.describe('移动视口冒烟', () => {
  test('对话工作区在移动视口可用', async ({ page }) => {
    await page.goto('/#qa')
    await expect(page.locator('.qa-empty')).toContainText('开始对话')
    await expect(page.getByRole('combobox', { name: '消息' })).toBeVisible()
    await expect(page.getByRole('button', { name: '发送' })).toBeVisible()
  })

  test('健康面板在移动视口可用', async ({ page }) => {
    await page.goto('/#system-status')
    await expect(page.getByRole('heading', { name: '系统状态' })).toBeVisible()
    await expect(page.getByText('本地服务运行正常')).toBeVisible()
  })
})
