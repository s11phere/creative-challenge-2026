import { expect, test } from '@playwright/test'

// Runs against the real Compose stack: the dashboard must show all four
// services available. The exact model-gateway status text is provider-dependent
// (fake stub vs configured external chat), so only its row presence is asserted.
test.describe('系统状态', () => {
  test('健康面板显示本地服务就绪', async ({ page }) => {
    await page.goto('/#system-status')
    await expect(page.getByRole('heading', { name: '系统状态' })).toBeVisible()
    await expect(page.getByText('本地服务运行正常')).toBeVisible()
    await expect(page.getByText('4 / 4 项当前可用')).toBeVisible()
    await expect(
      page.locator('.service-row', { hasText: 'PostgreSQL' }).getByText('可用'),
    ).toBeVisible()
    await expect(page.locator('.service-row', { hasText: '模型网关' })).toBeVisible()
  })
})
