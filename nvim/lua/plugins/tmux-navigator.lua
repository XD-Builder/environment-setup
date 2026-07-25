-- Seamless Alt-hjkl between Neovim splits and tmux panes.
-- Matches tmux @vim_navigator_mapping_*; default Ctrl-hjkl maps are disabled.
-- On VeryLazy, clear LazyVim's Alt-j/k move-line maps so they do not steal navigation.
return {
  {
    "christoomey/vim-tmux-navigator",
    lazy = false,
    init = function()
      vim.g.tmux_navigator_no_mappings = 1
    end,
    config = function()
      local opts = { silent = true, desc = "Tmux navigate" }
      vim.keymap.set({ "n", "t" }, "<M-h>", "<cmd>TmuxNavigateLeft<cr>", opts)
      vim.keymap.set({ "n", "t" }, "<M-j>", "<cmd>TmuxNavigateDown<cr>", opts)
      vim.keymap.set({ "n", "t" }, "<M-k>", "<cmd>TmuxNavigateUp<cr>", opts)
      vim.keymap.set({ "n", "t" }, "<M-l>", "<cmd>TmuxNavigateRight<cr>", opts)

      vim.api.nvim_create_autocmd("User", {
        pattern = "VeryLazy",
        once = true,
        callback = function()
          for _, mode in ipairs({ "n", "i", "v" }) do
            pcall(vim.keymap.del, mode, "<A-j>")
            pcall(vim.keymap.del, mode, "<A-k>")
          end
        end,
      })
    end,
  },
}
