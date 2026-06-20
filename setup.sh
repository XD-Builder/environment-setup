#!/bin/bash

shopt -s nocasematch

setup=ALL

if [ "$#" -eq 1 ]; then
  setup=$1
fi

OS_NAME=$(uname -s)

# {{{ Color utility functions
if which tput >/dev/null 2>&1; then
  colors=$(tput colors)
fi

if [ -t 1 ] && [ -n "$colors" ] && [ "$colors" -ge 8 ]; then
  # constants
  STYLE_NONE='\033[00m'
  COLOR_RED='\033[01;31m'
  COLOR_GREEN='\033[01;32m'
  COLOR_YELLOW='\033[01;33m'
  COLOR_PURPLE='\033[01;35m'
  COLOR_CYAN='\033[01;36m'
  COLOR_WHITE='\033[01;37m'
  FONT_BOLD='\033[1m'
  FONT_UNDERLINE='\033[4m'
else
  STYLE_NONE=''
  COLOR_RED=''
  COLOR_GREEN=''
  COLOR_YELLOW=''
  COLOR_PURPLE=''
  COLOR_CYAN=''
  COLOR_WHITE=''
  FONT_BOLD=''
  FONT_UNDERLINE=''
fi

# Prompts with enable backslash interpretation
prompt_bold() {
  echo -e "${STYLE_NONE} ${FONT_BOLD}$1${STYLE_NONE}"
}

prompt_error() {
  echo -e "${COLOR_RED}$1${STYLE_NONE}"
}

prompt_info() {
  echo -e "${COLOR_GREEN}$1${STYLE_NONE}"
}

prompt_select_print() {
  echo -e "${COLOR_PURPLE}$1${STYLE_NONE}${COLOR_GREEN} $2${STYLE_NONE}"
}

prompt_select_return() {
  echo -e "${COLOR_PURPLE}$1${STYLE_NONE}${COLOR_GREEN} [y/n]${STYLE_NONE}"
  read -r is_yes
  if [ "$is_yes" == "y" ]; then
    return 0
  else
    return 1
  fi
}

command_exists() {
  type "$1" &>/dev/null
}

# Resolve the installable package name for a given command (defaults to the command name)
package_name() {
  case "$1" in
  rg) echo "ripgrep" ;;
  *) echo "$1" ;;
  esac
}
# }}}
# {{{ OS Setup
# {{{ Mac setup
homebrew_setup() {
  declare -a commandsToInstall=("python3" "npm" "fzf" "rg" "tmux" "zsh")

  for command in "${commandsToInstall[@]}"; do
    command_exists "$command"
    if [ $? -eq 1 ]; then

      if prompt_select_return "command $command doesn't exist do you want to install?"; then
        prompt_info "installing command $command"
        brew install "$(package_name "$command")"
      fi
    fi
  done

  # Zsh autocomplete plugins (sourced from shell/.zshrc).
  declare -a zshPlugins=("zsh-autosuggestions" "zsh-syntax-highlighting" "zsh-completions")
  for plugin in "${zshPlugins[@]}"; do
    if ! brew list "$plugin" &>/dev/null; then
      if prompt_select_return "zsh plugin $plugin is not installed, install it?"; then
        prompt_info "installing $plugin"
        brew install "$plugin"
      fi
    fi
  done

  prompt_select_print "Do you want to install $(prompt_bold "vim")" "[m/v/n]${STYLE_NONE} - m for macvim, v for vim, n to skip"
  read -r vim_select
  if [ "$vim_select" == 'v' ]; then
    prompt_info "Installing $(prompt_bold "vim with python3") using brew"
    brew install vim
    if [ $? -eq 0 ]; then
      prompt_info "vim installed successful"
    fi
  elif [ "$vim_select" == 'm' ]; then
    prompt_info "Installing $(prompt_bold "macVim") using brew"
    brew install macvim
    if [ $? -eq 0 ]; then
      prompt_info "macVim installed successful"
    fi
  else
    prompt_info "skipping vim installation"
  fi
}

setup_mac() {
  if prompt_select_return "Do you want to set up $(prompt_bold "mac")"; then
    prompt_info "setting up homebrew"
    if /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
      prompt_info "mac set up successfully"
      homebrew_setup
    else
      prompt_error "mac set up failed, continuing setting up the environment"
    fi
  else
    prompt_error "skipping setting up mac"
  fi
}
# }}}
setup_linux() {
  declare -a commandsToInstall=("python3" "npm" "fzf" "rg" "tmux" "zsh")

  local PACKAGE_COMMAND=""
  if command_exists apt-get; then
    PACKAGE_COMMAND="apt-get"
  elif command_exists yum; then
    PACKAGE_COMMAND="yum"
  else
    prompt_error "Unable to find package command"
    return 1
  fi

  prompt_select_return "Detected package command $PACKAGE_COMMAND. Do you want to set up $(prompt_bold "linux")"
  if [ $? -ne 0 ]; then
    prompt_error "skipping setting up linux"
    return 1
  fi

  for command in "${commandsToInstall[@]}"; do
    if ! command_exists "$command"; then
      prompt_select_return "command $command doesn't exist do you want to install"
      if [ $? -eq 0 ]; then
        prompt_info "installing command $command"
        sudo $PACKAGE_COMMAND install "$(package_name "$command")"
      fi
    fi
  done

  prompt_select_print "Do you want to install $(prompt_bold "vim")" "[v/n]${STYLE_NONE} - v for vim, n to skip"
  read vim_select
  if [ "$vim_select" == 'v' ]; then
    prompt_info "Installing $(prompt_bold "vim") using $PACKAGE_COMMAND"
    sudo $PACKAGE_COMMAND install vim
    if [ $? -eq 1 ]; then
      prompt_error "unable to find package manager to install vim"
      return 1
    fi
  else
    prompt_info "skipping vim installation"
  fi
  return 0
}
# }}}
# {{{ Shell Setup
setup_shell() {
  prompt_select_print "Do you want to setup $(prompt_bold "bash or zsh")" "[b/z/n]${STYLE_NONE} - b for bash, z for zsh, n to skip"
  read -r shell_select
  cur_shell=$(basename $SHELL)
  if [ "$shell_select" == 'b' ]; then
    setup_bash "${cur_shell}"
  elif [ "$shell_select" == 'z' ]; then
    if ! setup_zsh ${cur_shell} ; then
      prompt_error "setup_zsh failed"
      exit 1
    fi
  else
    prompt_info "skipping shell installation"
  fi
}

setup_bash() {
  prompt_info "setting up bash profile"
  if [ -z "$1" ] || [ "$1" != "bash" ]; then
    prompt_select_return "Shell $1 is not bash, do you want to change your default shell to bash"
    if [ $? -eq 0 ]; then
      prompt_info "changing shell from $1 to bash"
      chsh -s $(which bash)
    fi
  fi

  if ln -sfn "${PWD}"/shell/.bashrc ~/.bash_profile; then
    prompt_info "bash set up successful"
  else
    prompt_error "error setting up bash profile"
    exit 1
  fi
  echo
}

setup_zsh() {
  prompt_info "setting up zsh profile"
  prompt_info "Downloading and installing ohmyzsh"
  mkdir -p "${PWD}"/zsh
  if [ -d "${PWD}"/zsh/oh-my-zsh ]; then
    git --git-dir="${PWD}"/zsh/oh-my-zsh/.git pull
  else
    if ! ZSH="${PWD}"/zsh/oh-my-zsh sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended --keep-zshrc; then
      prompt_error "Unable to install ohmyzsh"
      exit 1
    fi
  fi

  if [ -z "$1" ] || [ "$1" != "zsh" ]; then
    if prompt_select_return "Shell $1 is not zsh, do you want to change your default shell to zsh"; then
      prompt_info "changing shell from $1 to zsh"
      chsh -s $(which zsh)
    fi
  fi

  ln -sfn ${PWD}/shell/.zshrc ~/.zshrc && ln -sfn ${PWD}/shell/.zshrc.local ~/.zshrc.local && ln -sfn ${PWD}/zsh ~/.zsh

  if [ $? -eq 0 ]; then
    prompt_info "zsh set up successful"
  else
    prompt_error "error setting up zsh profile"
    exit 1
  fi
  echo
}

# }}}
# {{{ Editor utility setup
setup_vim() {
  prompt_select_return "Do you want to set up vim?"
  if [ $? -ne 0 ]; then
    return 2
  fi
  prompt_info "setting up vimrc and vim libraries"
  ln -sfn ${PWD}/vim/vimrc ~/.vimrc && ln -sfn ${PWD}/vim ~/.vim
  if [ $? -eq 0 ]; then
    prompt_info "vim folder setup successful"
  else
    prompt_info "error setting up vim"
    exit 1
  fi

  prompt_info "install plugins for vim (vim-plug self-installs on first launch)"
  vim +PlugInstall +qall
  echo
}

setup_tmux() {
  prompt_select_return "Do you want to set up tmux?"
  if [ $? -ne 0 ]; then
    return 0
  fi

  if [ ! -e ./tmux/plugins/tpm ]; then
    prompt_info "setting up tmux"
    git clone https://github.com/tmux-plugins/tpm ./tmux/plugins/tpm
    if [ $? -eq 0 ]; then
      prompt_info "tmux git download successful"
    else
      prompt_info "error setting up tmux"
      exit 1
    fi
  fi

  ln -sfn "${PWD}"/tmux/.tmux.conf ~/.tmux.conf && ln -sfn "${PWD}"/tmux ~/.tmux && ~/.tmux/plugins/tpm/scripts/install_plugins.sh
  if [ $? -eq 0 ]; then
    prompt_info "tmux setup successful"
  else
    prompt_info "error setting up tmux"
    exit 1
  fi

}
# }}}
# {{{ Main
case "$setup" in
"ALL")
  echo setting up all.
  # ======= MAIN =======
  ## Setup dependencies
  if [ "$OS_NAME" == "Darwin" ]; then
    setup_mac
  elif [ "$OS_NAME" == "Linux" ]; then
    setup_linux
  else
    prompt_error "$OS_NAME is supported for package setup"
    exit 1
  fi
  setup_shell
  setup_vim
  setup_tmux
  ;;
"tmux")
  echo setting up tmux
  setup_tmux
  ;;
"vim")
  echo setting up vim
  setup_vim
  ;;
"shell")
  echo setting up shell
  setup_shell
  ;;
"help")
  echo "Usage:"
  echo "./setup.sh [tmux|vim|shell]"
  ;;
esac

prompt_info "setup completed!"
# }}}
