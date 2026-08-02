// Posts real media-key events (NSSystemDefined / NX_KEYTYPE_*).
//
// The macOS volume keys are NOT regular key codes. AppleScript's
// `key code 72` is a 1980s Apple Extended Keyboard key, which is why
// synthesising it never reached MonitorControl — it listens for the
// media-key event, as every other volume observer does. Brightness
// happened to work because 144/145 are legacy codes macOS still maps.
//
// Build: clang -framework Foundation -framework AppKit -o mediakey mediakey.m
#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <IOKit/hidsystem/ev_keymap.h>

static void postKey(int key, BOOL down, BOOL fine) {
    // shift+option is macOS's fine-adjust modifier; MonitorControl honours it
    // for volume exactly as it does for brightness (1/4 notch instead of 1/16).
    NSUInteger mods = (down ? 0xA00 : 0xB00);
    if (fine) mods |= NSEventModifierFlagShift | NSEventModifierFlagOption;
    NSEvent *e = [NSEvent otherEventWithType:NSEventTypeSystemDefined
                                    location:NSZeroPoint
                               modifierFlags:mods
                                   timestamp:0
                                windowNumber:0
                                     context:nil
                                     subtype:8
                                       data1:((key << 16) | ((down ? 0xA : 0xB) << 8))
                                       data2:-1];
    CGEventPost(kCGHIDEventTap, [e CGEvent]);
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc < 2) { fprintf(stderr, "usage: mediakey up|down|mute|brightup|brightdown [count] [fine]\n"); return 2; }
        int key = NX_KEYTYPE_SOUND_UP;
        if (strcmp(argv[1], "down") == 0) key = NX_KEYTYPE_SOUND_DOWN;
        else if (strcmp(argv[1], "mute") == 0) key = NX_KEYTYPE_MUTE;
        else if (strcmp(argv[1], "brightup") == 0) key = NX_KEYTYPE_BRIGHTNESS_UP;
        else if (strcmp(argv[1], "brightdown") == 0) key = NX_KEYTYPE_BRIGHTNESS_DOWN;
        int n = (argc > 2) ? atoi(argv[2]) : 1;
        BOOL fine = (argc > 3 && strcmp(argv[3], "fine") == 0);
        for (int i = 0; i < n; i++) { postKey(key, YES, fine); postKey(key, NO, fine); usleep(40000); }
    }
    return 0;
}
