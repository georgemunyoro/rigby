#define SDL_MAIN_HANDLED

#ifdef _WIN32
#pragma comment(lib, "Ws2_32.lib")
#endif

#include <SDL2/SDL.h>
#include <imgui.h>
#include <imgui_impl_sdl2.h>
#include <imgui_impl_sdlrenderer2.h>
#include <iostream>

#include "application.h"

int main(int argc, char *argv[]) {
  if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO) < 0) {
    std::cout << "SDL_Init failed: " << SDL_GetError() << std::endl;
    return -1;
  }

  SDL_Window *window_ptr =
      SDL_CreateWindow("SDL2 Window", SDL_WINDOWPOS_CENTERED,
                       SDL_WINDOWPOS_CENTERED, 1000, 750, SDL_WINDOW_SHOWN);
  if (window_ptr == nullptr) {
    SDL_Quit();
    return -1;
  }

  SDL_Renderer *renderer_ptr =
      SDL_CreateRenderer(window_ptr, -1, SDL_RENDERER_ACCELERATED);
  if (renderer_ptr == nullptr) {
    SDL_DestroyWindow(window_ptr);
    SDL_Quit();
    return -1;
  }

  IMGUI_CHECKVERSION();
  ImGui::CreateContext();
  ImGuiIO &io = ImGui::GetIO();

  ImGui_ImplSDL2_InitForSDLRenderer(window_ptr, renderer_ptr);
  ImGui_ImplSDLRenderer2_Init(renderer_ptr);

  Application &app = Application::get_instance();
  app.initialize(renderer_ptr);

  bool running = true;
  while (running) {
    SDL_Event e;
    while (SDL_PollEvent(&e)) {
      if (e.type == SDL_QUIT) {
        running = false;
      }
      ImGui_ImplSDL2_ProcessEvent(&e);
    }

    ImGui_ImplSDLRenderer2_NewFrame();
    ImGui_ImplSDL2_NewFrame();
    ImGui::NewFrame();
    app.render();
    ImGui::Render();

    SDL_SetRenderDrawColor(renderer_ptr, 0, 0, 0, 255);
    SDL_RenderClear(renderer_ptr);
    ImGui_ImplSDLRenderer2_RenderDrawData(ImGui::GetDrawData(), renderer_ptr);
    SDL_RenderPresent(renderer_ptr);
  }

  ImGui_ImplSDLRenderer2_Shutdown();
  ImGui_ImplSDL2_Shutdown();
  ImGui::DestroyContext();

  SDL_DestroyRenderer(renderer_ptr);
  SDL_DestroyWindow(window_ptr);
  SDL_Quit();

  return 0;
}
