return {
  -- Wombat port based on wombat256mod (matches existing vim colorscheme)
  {
    "ViViDboarder/wombat.nvim",
    lazy = false,
    priority = 1000,
    dependencies = { "rktjmp/lush.nvim" },
  },

  -- Configure LazyVim to load wombat
  {
    "LazyVim/LazyVim",
    opts = {
      colorscheme = "wombat",
    },
  },
}
