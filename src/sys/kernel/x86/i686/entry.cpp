#include <kernel/boot/boot_file.h>
#include <kernel/boot/multiboot.h>
#include <kernel/config.h>
#include <kernel/hal/fb_text_console.h>
#include <kernel/hal/sw_framebuffer.h>
#include <kernel/log.h>
#include <kernel/mm/heap.h>
#include <kernel/mm/pmm.h>
#include <kernel/mm/vmm.h>
#include <kernel/panic.h>
#include <kernel/time.h>
#include <kernel/types.h>
#include <kernel/x86/descriptor_table.h>
#include <kernel/x86/interrupts.h>
#include <kernel/x86/paging.h>
#include <kernel/x86/pit.h>
#include <kernel/x86/serial_text_console.h>
#include <kernel/x86/vga_text_console.h>
#include <string.h>

extern "C" void      _halt();
extern "C" uint32_t* boot_page_directory;
extern "C" uint32_t* boot_page_table1;
extern "C" uint32_t* _kernel_end;

x86_idt                             g_idt;
kernel::device::vga_text_console    con_vga;
kernel::device::serial_text_console con_serial;
page_directory                      boot_directory;
kernel::boot_file_container         kernel::g_bootfiles;

void kernel_main();
void init_global_constructors();
void kernel_print_version() { kernel::log::info("kernel", "Umbra v. %s on x86 (i686)\n", KERNEL_VERSION); }

void boot_init_log() {
    auto& log = kernel::log::get();
    log.init(&con_serial);
    log.shouldBuffer = false;  // Disable buffering for now
}

void boot_init_memory(multiboot_info_t* mb_info) {
    boot_directory                = page_directory((page_directory_raw_t*)(&boot_page_directory));
    boot_directory.directory_addr = (uint32_t)(&boot_page_directory) - 0xC0000000;
    boot_directory.pt_virt[768]   = (uint32_t)(&boot_page_table1);

    // Parse the multiboot memory map for available regions
    auto* mb_mmap = (multiboot_memory_map_t*)(mb_info->mmap_addr + 0xC0000000);
    for (; (uint32_t)mb_mmap < (mb_info->mmap_addr + 0xC0000000) + mb_info->mmap_length;
         mb_mmap = (multiboot_memory_map_t*)((uint32_t)mb_mmap + mb_mmap->size + sizeof(mb_mmap->size))) {
        uint32_t                addr     = (uint32_t)mb_mmap->addr;
        uint32_t                end_addr = (uint32_t)(mb_mmap->addr + mb_mmap->len - 1);
        kernel::pmm_region_type type     = kernel::pmm_region_type::unknown;
        if (mb_mmap->type == MULTIBOOT_MEMORY_AVAILABLE) { type = kernel::pmm_region_type::ram; }
        kernel::g_pmm.add_region(kernel::pmm_region(type, addr, end_addr));
    }
    kernel::g_vmm.dir_current = &boot_directory;
    kernel::g_pmm.init();
    g_heap.init(false, (uint32_t)(&_kernel_end));
}

void boot_init_modules(multiboot_info_t* mb_info) {
    // Now we need to find the initial ramdisk...
    phys_addr_t mod_phys = mb_info->mods_addr;
    virt_addr_t mod_virt = 0;

    for (size_t i = 0; i < mb_info->mods_count; i++) {
        if (mod_phys < 0x100000) {
            // Loaded in lower half -> must be in virtual memory already
            mod_virt = mod_phys + 0xC0000000;
        }

        // Map this into the heap area
        auto mod = (multiboot_module_t*)mod_virt;

        auto bfile  = kernel::boot_file();
        bfile.name  = (char const*)mod->cmdline + 0xC0000000;
        bfile.paddr = mod->mod_start;
        bfile.size  = mod->mod_end - mod->mod_start;

        virt_addr_t placement_addr = g_heap.get_placement();
        placement_addr &= 0xFFFFF000;
        bfile.vaddr = placement_addr + 0x1000;

        for (uintptr_t p = mod->mod_start; p <= mod->mod_end; p += 0x1000) {
            placement_addr += 0x1000;
            kernel::g_vmm.mmap_direct(placement_addr, p, 0x03);
        }
        g_heap.set_placmement(placement_addr);
        kernel::log::trace("boot", "loaded file %s: sz:%d 0x%08x -> 0x%08x\n", bfile.name, bfile.size, bfile.paddr,
                           bfile.vaddr);
        kernel::g_bootfiles.add(bfile);
        mod_phys += sizeof(multiboot_module_t);
    }
}

/// The responsibility of the kernel_entry function is to initialse the system into the minimuim startup state.
/// All architecture specific core functions (Tables, Paging, APs, Display) should be setup before control is transfered
/// to kernel_main
extern "C" void kernel_entry(uint32_t mb_magic, multiboot_info_t* mb_info) {
    kernel::device::fb_text_console con_fb;

    init_global_constructors();
    auto& log = kernel::log::get();
    boot_init_log();  // Setup the log

    // Check that we can actually trust the boot enviroment
    if (mb_magic != 0x2BADB002) {
        kernel::log::critical("multiboot", "Multiboot magic was 0x%08x, halting!\n", mb_magic);
        panic("Multiboot magic incorrect");
    }

    kernel::log::debug("kernel", "Alive!\n");

    boot_init_memory(mb_info);   // Initialise the memory map (get it from GRUB)
    kernel::x86::g_gdt.init();   // Initialise the GDT
    g_idt.init();                // Initialise the IDT
    g_idt.enable_interrupts();   // Start tracking interrupts (scheduling is diabled)
    boot_init_modules(mb_info);  // Locate and load in the modules

    // Initialise the display
    if (mb_info->framebuffer_type == 2) {
        kernel::log::debug("display", "Using VGA 80x25 textmode\n");
        log.init(&con_vga);
    } else {
        kernel::log::debug("display", "Recieved framebuffer: %dx%dx%d @ 0x%p from multiboot\n", mb_info->framebuffer_width,
                           mb_info->framebuffer_height, mb_info->framebuffer_bpp, mb_info->framebuffer_addr);

        fb_format display_format = fb_format::rgb;

        if (mb_info->framebuffer_red_field_position == 16) { display_format = fb_format::bgr; }

        for (size_t i = 0; i < mb_info->framebuffer_height * mb_info->framebuffer_pitch; i += 0x1000) {
            kernel::g_vmm.mmap_direct((virt_addr_t)mb_info->framebuffer_addr + i, (phys_addr_t)mb_info->framebuffer_addr + i,
                                      0x03);
        }

        con_fb.framebuffer =
            sw_framebuffer((uint8_t*)mb_info->framebuffer_addr, mb_info->framebuffer_width, mb_info->framebuffer_height,
                           mb_info->framebuffer_bpp, mb_info->framebuffer_pitch, display_format);
        log.init(&con_fb);
    }

    kernel_print_version();  // Print the version of the kernel

    // Initialise a timer
    pit_timer timer_pit;
    timer_pit.init();
    kernel::time::system_timer = &timer_pit;

    // Call into the kernel now that all supported hardware is initialised.
    kernel_main();
}
