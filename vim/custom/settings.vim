let mapleader = ","

let &backspace="indent,eol,start"                              " Fix backspace deletion
let &clipboard = has('unnamedplus') ? 'unnamedplus' : 'unnamed' " Check have clipboard
set wildignore+=*/tmp/*,*.so,*.swp,*.zip,*.pyc,*.db,*.sqlite,*.o,*.obj,.git,*.rbc,*.pyc,__pycache__

colorscheme wombat256mod                                        " Search ftp/colors/{name}.vim and load the first one
set timeoutlen=1000                                             " Give 1 seconds timeout for key code or mapped key sequence to complete
set autoread                                                    " Read the file automatically if changed outside the buffer
set autowriteall                                                " Write if content has been modified for all command
set copyindent                                                  " Copy the previous indentation on autoindenting. For <tab> mostly
set cursorline                                                  " Highlight the screen line of the cursor with CursorLine
set encoding=utf-8                                              " Set the character encoding inside Vim, doesn't modify existing file encoding
set expandtab                                                   " spaces for tab <c-v><tab> if want realtab
set fileencoding=utf-8                                          " Encoding for when saving a file
set fileencodings=utf-8                                         " Encodings list used for determining encoding during read file start
set fileformats=unix,dos,mac                                    " Automatically detect and set file formats / EOL for you
set foldmethod=marker                                           " set fold to marker, triple { will create a fold
set hidden                                                      " Keep buffer when a buffer is off a window
set history=700                                                 " Number of lines that are remember
set hlsearch                                                    " highlights the all search matches for a pattern
set ignorecase                                                  " Ignore case when searching
set incsearch                                                   " show search result as you type
set mouse=a                                                     " Enable the use of the mouse for all modes
set noautoindent                                                " Return to indent of previous line
set nobackup                                                    " No backup file when overwriting a file
set noswapfile                                                  " buffer loaded without swp file, avoid security issues if confidential info
set nowritebackup                                               " No backup on write a file, default vim creates backup file to prevent loss on write
set nrformats=                                                  " c-a or c-x on normal mode. No alpha, octal, hex, bin increment/decrement
set number                                                      " Displays line number in front of each line
set ruler                                                       " Show line and column number of the cursor position
set scrolloff=2                                                 " Always show minimum 2 screen lines before/after the cursor
set shiftround                                                  " C-T, C-D in insert mode or <, > in normal mode to indent multiple of 'shiftwidth'
set shiftwidth=4                                                " Set number of spaces to use for each step of indent, >>, <<
set showcmd                                                     " Show partial command in the last line of the screen
set smartcase                                                   " Search only exact match when capital letter is provided else all. Overrides ignorecase
set smartindent                                                 " Smarter autoindent when starting a new line enabled for languages
set softtabstop=4                                               " An addition to tabstop for tab while performing editing operations
" set spell spelllang=en_us                                       " Set spell to USA. It checks files for spell error
set tabstop=4                                                   " Number of spaces a <tab> counts for
set tags=./tags,tags;$HOME                                      " For ctag with completion, C-] get into the tag and C-T get out of the tag
if !has('nvim')
    if has("mouse_sgr")
        set ttymouse=sgr
    else
        set ttymouse=xterm2
    end
endif
set undolevels=700                                              " Max number of changes that can be undone
if has('persistent_undo')
    if !isdirectory($HOME . '/.vim/undo')
        call mkdir($HOME . '/.vim/undo', 'p', 0700)
    endif
    set undodir=~/.vim/undo                                     " Keep undo history on disk
    set undofile                                                " Persist undo across sessions
endif
set viminfo='0,:0,<0,@0,f0                                      " Used to remember information, such as CLI, search, marks, etc. Set to 0 for all
set wildmenu                                                    " Enable enhanced mode where pressing <Tab> to invoke completion.
set wildmode=list:longest,list:full                             " Complete all match till longest common string and complete first match
set secure                                                      " Disallow shell and write commands in vimrc files
