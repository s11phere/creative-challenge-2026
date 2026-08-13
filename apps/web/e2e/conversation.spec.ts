import { expect, test } from '@playwright/test'
import { expectFinalAnswer, sendMessage } from './helper.js'

// All prompts are synthetic; assertions use the deterministic fake-provider
// outputs of the Compose stack. The DB is empty (fresh volume), so the
// knowledge question deterministically refuses.
test.describe('助手对话', () => {
  test('问候获得确定性终态回答并显示运行卡片', async ({ page }) => {
    await page.goto('/#qa')
    await sendMessage(page, '你好')
    await expectFinalAnswer(page, 'fake-response-autonomous')
    await expect(page.getByLabel('Agent 运行时间线')).toBeVisible()
    await expect(page.locator('.chat-agent-timeline-status')).toContainText('已完成')
  })

  test('空会话显示占位且发送按钮禁用', async ({ page }) => {
    await page.goto('/#qa')
    await expect(page.locator('.qa-empty')).toContainText('开始对话')
    await expect(page.getByRole('button', { name: '发送' })).toBeDisabled()
  })

  test('提交失败时显示错误提示', async ({ page }) => {
    await page.goto('/#qa')
    // Deterministic failure: abort the turn POST (conversation creation still works).
    await page.route('**/api/v2/conversations/*/turns', (route) => route.abort())
    await sendMessage(page, '你好')
    await expect(page.getByRole('alert')).toBeVisible()
  })
})
