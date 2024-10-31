#include "application.h"
#include "OpenRGB/Client.hpp"
#include "OpenRGB/DeviceInfo.hpp"

#include <imgui.h>
#include <spdlog/spdlog.h>
#include <stdexcept>

void Application::initialize(SDL_Renderer *renderer) {
  if (renderer == nullptr) {
    spdlog::error("Renderer is null");
    throw std::invalid_argument("Renderer is null");
  }
  this->renderer = renderer;
}

void Application::render() {
  ImGui::Begin("Application");
  if (ImGui::BeginTabBar("Tabs")) {
    if (ImGui::BeginTabItem("Devices")) {
      state.selected_tab = ViewState::SelectedTab::Devices;
      render_device_tab();
      ImGui::EndTabItem();
    }

    if (ImGui::BeginTabItem("Effects")) {
      state.selected_tab = ViewState::SelectedTab::Effects;
      ImGui::EndTabItem();
    }

    if (ImGui::BeginTabItem("Settings")) {
      state.selected_tab = ViewState::SelectedTab::Settings;
      ImGui::EndTabItem();
    }

    ImGui::EndTabBar();
  }

  ImGui::End();
}

void Application::render_device_tab() {
  const auto &devices = device_controller.get_devices();
  ImGui::Text("%zu devices found.", devices.size());

  ImGui::BeginChild("##Devices",
                    ImVec2(300.0f, ImGui::GetContentRegionAvail().y),
                    ImGuiChildFlags_None, 0);
  render_device_tab__device_select(devices);
  ImGui::EndChild();

  ImGui::SameLine();

  ImGui::BeginChild("##Device Details", ImVec2(0, -1), ImGuiChildFlags_None, 0);
  render_device_tab__device_info(devices);
  ImGui::EndChild();
}

void Application::render_device_tab__device_select(
    const orgb::DeviceList &devices) {
  if (!ImGui::BeginListBox("##Devices List Box", ImVec2(-1, -1)))
    return;

  for (const auto &device : devices) {
    const auto unique_name_str =
        device_controller.get_device_unique_name(device);
    const auto unique_name = unique_name_str.c_str();

    const bool is_selected = device.idx == state.devices.selected_device;
    const bool is_highlighted = device.idx == state.devices.highlighted_device;

    if (ImGui::Selectable(unique_name, is_selected))
      state.devices.selected_device = device.idx;

    if (ImGui::IsItemHovered())
      state.devices.highlighted_device = device.idx;

    if (is_selected)
      ImGui::SetItemDefaultFocus();
  }

  ImGui::EndListBox();
}

void Application::render_device_tab__device_info(
    const orgb::DeviceList &devices) {
  const auto &device = devices[state.devices.selected_device];
  const auto unique_name_str = device_controller.get_device_unique_name(device);
  const auto unique_name = unique_name_str.c_str();

  ImGui::Text("Name: %s", unique_name);
  ImGui::Text("Type: %s", orgb::enumString(device.type));
  ImGui::Separator();

  if (!ImGui::BeginChild("##Device Info LEDs", ImVec2(0, -1), false, 0))
    return;

  for (const auto &led : device.leds) {
    const auto led_name = led.name.c_str();
    ImGui::Text("Name: %s", led_name);
  }

  ImGui::EndChild();
}
