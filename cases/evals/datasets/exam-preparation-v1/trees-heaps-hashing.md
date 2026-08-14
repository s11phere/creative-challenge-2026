# 树、堆与哈希讲义

二叉搜索树满足左子树键小于节点、右子树键大于节点，因此中序遍历得到有序序列；若树退化，查找最坏为 O(n)。平衡搜索树把高度维持在 O(log n)。二叉堆只保证父子之间的偏序，取最值 O(1)，插入和删除最值 O(log n)，不支持任意元素的高效查找。哈希表在负载因子受控且散列良好时，查找期望 O(1)；冲突可用拉链或开放寻址处理。

许可：CC0 1.0。用途：public_demo、repository_fixture、retrieval、question_generation、evaluation。
