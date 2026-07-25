-- Seamless Alt-hjkl between Neovim splits and tmux panes.
-- Matches tmux @vim_navigator_mapping_*; default Ctrl-hjkl maps are disabled.
--
-- LazyVim registers Alt-j/k (move line) on VeryLazy. Delete + remap must run
-- after that — vim.schedule defers past other VeryLazy handlers in the same tick.
return {
  {
    "christoomey/vim-tmux-navigator",
    lazy = false,
    init = function()
      vim.g.tmux_navigator_no_mappings = 1
    end,
    config = function()
      vim.api.nvim_create_autocmd("User", {
        pattern = "VeryLazy",
        once = true,
        callback = function()
          vim.schedule(function()
            -- LazyVim uses <A-j>/<A-k>; clear those so they do not steal Alt navigation.
            for _, mode in ipairs({ "n", "i", "v" }) do
              pcall(vim.keymap.del, mode, "<A-j>")
              pcall(vim.keymap.del, mode, "<A-k>")
            end

            local opts = { silent = true, desc = "Tmux navigate" }
            local maps = {
              ["<M-h>"] = "TmuxNavigateLeft",
              ["<M-j>"] = "TmuxNavigateDown",
              ["<M-k>"] = "TmuxNavigateUp",
              ["<M-l>"] = "TmuxNavigateRight",
            }
            for key, cmd in pairs(maps) do
              vim.keymap.set({ "n", "t" }, key, "<cmd>" .. cmd .. "<cr>", opts)
            end
          end)
        end,
      })
    end,
  },
}
