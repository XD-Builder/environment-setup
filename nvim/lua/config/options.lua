-- Options are automatically loaded before lazy.nvim startup
-- Default options that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/options.lua

-- Match existing vim leader (LazyVim default is <space>)
vim.g.mapleader = ","
vim.g.maplocalleader = "\\"

-- Prefer pyright for the LazyVim python extra
vim.g.lazyvim_python_lsp = "pyright"

local opt = vim.opt

opt.number = true
opt.cursorline = true
opt.expandtab = true
opt.shiftwidth = 4
opt.tabstop = 4
opt.softtabstop = 4
opt.ignorecase = true
opt.smartcase = true
opt.hlsearch = true
opt.incsearch = true
opt.clipboard = vim.fn.has("unnamedplus") == 1 and "unnamedplus" or "unnamed"
opt.swapfile = false
opt.backup = false
opt.writebackup = false
opt.foldmethod = "marker"
opt.scrolloff = 2
opt.mouse = "a"
opt.autoread = true
opt.hidden = true

-- Persistent undo (Neovim default state dir)
local undodir = vim.fn.stdpath("state") .. "/undo"
if vim.fn.isdirectory(undodir) == 0 then
  vim.fn.mkdir(undodir, "p", "0700")
end
opt.undodir = undodir
opt.undofile = true
