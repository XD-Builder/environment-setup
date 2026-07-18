-- Seamless Ctrl-hjkl navigation between Neovim splits and tmux panes.
-- LazyVim's default <C-h/j/k/l> window maps are overridden by these keys.
return {
  {
    "christoomey/vim-tmux-navigator",
    cmd = {
      "TmuxNavigateLeft",
      "TmuxNavigateDown",
      "TmuxNavigateUp",
      "TmuxNavigateRight",
      "TmuxNavigatePrevious",
    },
    keys = {
      { "<c-h>", "<cmd>TmuxNavigateLeft<cr>", desc = "Tmux left" },
      { "<c-j>", "<cmd>TmuxNavigateDown<cr>", desc = "Tmux down" },
      { "<c-k>", "<cmd>TmuxNavigateUp<cr>", desc = "Tmux up" },
      { "<c-l>", "<cmd>TmuxNavigateRight<cr>", desc = "Tmux right" },
    },
  },
}
