-- Keymaps are automatically loaded on the VeryLazy event
-- Default keymaps: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/keymaps.lua
-- Core parity with environment-setup vim bindings only.

local map = vim.keymap.set

-- Save / quit
map({ "n", "i" }, "<C-c>", "<esc><cmd>write<cr>", { desc = "Save file" })
map("n", "<leader>x", "<cmd>update<cr>", { desc = "Save file" })
map("i", "jk", "<esc><cmd>update<cr>", { desc = "Escape and save" })
map("n", "<leader>e", "<cmd>quit<cr>", { desc = "Quit" })
map("n", "<leader>E", "<cmd>qa!<cr>", { desc = "Quit all (force)" })

-- Clear search highlight
map("n", "<leader>/", "<cmd>nohlsearch<cr>", { desc = "Clear search highlight" })

-- File tree (neo-tree) — mirrors vim ,z NERDTreeToggle
map("n", "<leader>z", "<cmd>Neotree toggle<cr>", { desc = "Toggle file tree" })

-- Fuzzy find (Snacks picker / LazyVim.pick)
map("n", "<C-p>", LazyVim.pick("files"), { desc = "Find files" })
map("n", "<leader>pf", LazyVim.pick("files"), { desc = "Find files" })
map("n", "<leader>pb", LazyVim.pick("buffers"), { desc = "Buffers" })
map("n", "<leader>pr", LazyVim.pick("grep"), { desc = "Ripgrep" })

-- Tabs
map("n", "<leader>tt", "<cmd>tabnew<cr>", { desc = "New tab" })
map("n", "<leader>tn", "<cmd>tabnext<cr>", { desc = "Next tab" })
map("n", "<leader>tp", "<cmd>tabprevious<cr>", { desc = "Previous tab" })
map("n", "<leader>tc", "<cmd>tabclose<cr>", { desc = "Close tab" })

-- Buffers
map("n", "<leader>bn", "<cmd>bnext<cr>", { desc = "Next buffer" })
map("n", "<leader>bp", "<cmd>bprevious<cr>", { desc = "Previous buffer" })
map("n", "<leader>bc", "<cmd>bd<cr>", { desc = "Close buffer" })

-- Window resize (vim-style ,w{h,j,k,l})
map("n", "<leader>wh", function()
  vim.cmd("vertical resize " .. math.floor(vim.fn.winwidth(0) * 2 / 3))
end, { desc = "Shrink width" })
map("n", "<leader>wl", function()
  vim.cmd("vertical resize " .. math.floor(vim.fn.winwidth(0) * 3 / 2))
end, { desc = "Grow width" })
map("n", "<leader>wj", function()
  vim.cmd("resize " .. math.floor(vim.fn.winheight(0) * 3 / 2))
end, { desc = "Grow height" })
map("n", "<leader>wk", function()
  vim.cmd("resize " .. math.floor(vim.fn.winheight(0) * 2 / 3))
end, { desc = "Shrink height" })

-- Visual indent keep selection
map("v", "<Tab>", ">gv", { desc = "Indent" })
map("v", "<S-Tab>", "<gv", { desc = "Outdent" })
