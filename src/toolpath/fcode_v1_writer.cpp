#include <stdio.h>
#include <math.h>
#include <stdexcept>
#include <sstream>
#include "crc32.c"
#include "fcode.h"

void FLUX::FCodeV1Base::write(const char* buf, size_t size, unsigned long *crc32_ptr) {
    stream->write(buf, size);
    if(crc32_ptr) {
        *crc32_ptr = crc32(*crc32_ptr, (const void *)buf, size);
        // *crc32_ptr = crc32(*crc32_ptr, (const Bytef*)buf, size);
    }
}

void FLUX::FCodeV1Base::write(float value, unsigned long *crc32) {
    write((const char *)&value, 4, crc32);
}

void FLUX::FCodeV1Base::write(uint32_t value, unsigned long *crc32) {
    write((const char *)&value, sizeof(uint32_t), crc32);
}


void FLUX::FCodeV1Base::write_command(unsigned char cmd, unsigned long *crc32) {
    write((const char *)&cmd, 1, crc32);
}


void FLUX::FCodeV1Base::moveto(int flags, float feedrate, float x, float y, float z, float e0, float e1, float e2) {
    write_command(flags | 128, &script_crc32);

    if(flags & FLAG_HAS_FEEDRATE && feedrate > 0) write(feedrate, &script_crc32);
    if(flags & FLAG_HAS_X) write(x, &script_crc32);
    if(flags & FLAG_HAS_Y) write(y, &script_crc32);
    if(flags & FLAG_HAS_Z) write(z, &script_crc32);
    if(flags & FLAG_HAS_E(0)) write(e0, &script_crc32);
    if(flags & FLAG_HAS_E(1)) write(e1, &script_crc32);
    if(flags & FLAG_HAS_E(2)) write(e2, &script_crc32);
}

void FLUX::FCodeV1Base::sleep(float seconds) {
    write_command(4, &script_crc32);
    write(seconds * 1000, &script_crc32);
}

void FLUX::FCodeV1Base::enable_motor(void) { errors.push_back(std::string("NOT_SUPPORT ENABLE_MOTOR")); }

void FLUX::FCodeV1Base::disable_motor(void) { errors.push_back(std::string("NOT_SUPPORT DISABLE_MOTOR")); }

void FLUX::FCodeV1Base::pause(bool to_standby_position) {
    write_command((to_standby_position ? 5 : 6), &script_crc32);
}

void FLUX::FCodeV1Base::home(void) {
    write_command(1, &script_crc32);
}
void FLUX::FCodeV1Base::set_toolhead_heater_temperature(float temperature, bool wait) {
    write_command(wait ? 24 : 16, &script_crc32);
    write(temperature, &script_crc32);
}
void FLUX::FCodeV1Base::set_toolhead_fan_speed(float strength) {
    write_command(48, &script_crc32);
    write(strength, &script_crc32);
}
void FLUX::FCodeV1Base::set_toolhead_pwm(float strength) {
    write_command(32, &script_crc32);
    write(strength, &script_crc32);
}

void FLUX::FCodeV1Base::append_anchor(uint32_t value) {}
void FLUX::FCodeV1Base::append_comment(const char* message, size_t length) {}
void FLUX::FCodeV1Base::on_error(bool critical, const char* message, size_t length) {
    if(critical) {
        errors.push_back(std::string("ERROR ") + std::string(message, length));
    } else {
        errors.push_back(std::string("WARNING ") + std::string(message, length));
    }
}


FLUX::FCodeV1::FCodeV1(std::string *type, std::vector<std::pair<std::string, std::string> > *file_metadata, std::vector<std::string> *image_previews) {
    home_x = 0; home_y = 0, home_z = 240;
    current_feedrate = 0;
    current_x = 0; current_y = 0; current_z = 0;
    travled = time_cost = 0;
    max_x = max_y = max_z = max_r = filament[0] = filament[1] = filament[2] = 0;

    head_type = type;
    metadata = file_metadata;
    previews = image_previews;
    script_crc32 = 0;
}

void FLUX::FCodeV1::begin(void) {
    write("FCx0001\n", 8, NULL);
    script_offset = stream->tellp();
    if(script_offset < 0) {
        throw std::runtime_error("NOT_SUPPORT STREAM");
    }
    write("\x00\x00\x00\x00", 4, NULL);
}

void FLUX::FCodeV1::moveto(int flags, float feedrate, float x, float y, float z, float e0, float e1, float e2) {
    if(flags & FLAG_HAS_FEEDRATE && feedrate > 0) {
        current_feedrate = feedrate;
    }

    bool has_move = false;
    float mv[3] = {0, 0, 0};
    float fm[3] = {0, 0, 0};

    if(flags & FLAG_HAS_X) {
        mv[0] = x - current_x;
        current_x = x;
        max_x = fmax(max_x, x);
        has_move = true;
    }
    if(flags & FLAG_HAS_Y) {
        mv[1] = y - current_y;
        current_y = y;
        max_y = fmax(max_y, y);
        has_move = true;
    }
    if(flags & FLAG_HAS_X || flags & FLAG_HAS_Y) {
        float r = sqrtf(pow(current_x, 2) + pow(current_y, 2));
        max_r = fmax(max_r, r);
        has_move = true;
    }
    if(flags & FLAG_HAS_Z) {
        mv[2] = z - current_z;
        current_z = z;
        max_z = fmax(max_z, z);
        has_move = true;
    }
    if(flags & FLAG_HAS_E(0)) { fm[0] = filament[0] - e0; filament[0] = e0; }
    if(flags & FLAG_HAS_E(1)) { fm[1] = filament[1] - e1; filament[1] = e1; }
    if(flags & FLAG_HAS_E(2)) { fm[2] = filament[2] - e2; filament[2] = e2; }

    if(has_move) {
        double dist = sqrt(pow(mv[0], 2) + pow(mv[1], 2) + pow(mv[2], 2));
        if(!isnan(dist)) {
            travled += dist;
            if(current_feedrate > 0) {
                float tc = (dist / current_feedrate) * 60.0;
                if(!isnan(tc)) time_cost += tc;
            } else {
                on_error(false, "BAD_FEEDRATE", 14);
            }
        }
    } else {
        float tc = (fmax(fmax(fm[0], fm[1]), fm[2]) / feedrate) * 60.0;
        if(!isnan(tc)) time_cost += tc;
    }
    FCodeV1Base::moveto(flags, feedrate, x, y, z, e0, e1, e2);
}
void FLUX::FCodeV1::sleep(float seconds) {
    if(!isnan(seconds)) time_cost += seconds;
    FCodeV1Base::sleep(seconds);
}
void FLUX::FCodeV1::home(void) {
    current_x = home_x; current_y = home_y; current_z = home_z;
    FCodeV1Base::home();
}

unsigned long FLUX::FCodeV1::write_metadata(void) {
    char metabuf[128];
    int metasize;
    unsigned long metadata_crc32 = 0;

    if(filament[2]) {
        metasize = snprintf(metabuf, 128, "%.2f,%.2f,%.2f", filament[0], filament[1], filament[2]);
    } else if(filament[1]) {
        metasize = snprintf(metabuf, 128, "%.2f,%.2f", filament[0], filament[1]);
    } else {
        metasize = snprintf(metabuf, 128, "%.2f", filament[0]);
    }
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("FILAMENT_USED", std::string(metabuf, metasize)));

    metasize = snprintf(metabuf, 32, "%.2f", max_r + 0.2);
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("MAX_R", std::string(metabuf, metasize)));

    metasize = snprintf(metabuf, 32, "%.2f", max_z + 0.2);
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("MAX_Z", std::string(metabuf, metasize)));

    metasize = snprintf(metabuf, 32, "%.2f", max_y + 0.2);
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("MAX_Y", std::string(metabuf, metasize)));

    metasize = snprintf(metabuf, 32, "%.2f", max_x + 0.2);
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("MAX_X", std::string(metabuf, metasize)));

    metasize = snprintf(metabuf, 32, "%.2f", travled);
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("TRAVEL_DIST", std::string(metabuf, metasize)));

    metasize = snprintf(metabuf, 32, "%.2f", time_cost);
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("TIME_COST", std::string(metabuf, metasize)));

    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("HEAD_TYPE", *head_type));
    metadata->insert(metadata->begin(),
        std::pair<std::string, std::string>("VERSION", "1"));

    for(auto it=metadata->begin();it!=metadata->end();++it) {
    // for(auto it : *metadata) {
        write(it->first.data(), it->first.size(), &metadata_crc32);
        write("=", 1, &metadata_crc32);
        write(it->second.data(), it->second.size(), &metadata_crc32);
        write("\x00", 1, &metadata_crc32);
    }
    return metadata_crc32;
}

void FLUX::FCodeV1::terminated(void) {
    uint32_t u32value;

    int script_end_offset = stream->tellp();
    stream->seekp(script_offset, stream->beg);
    u32value = script_end_offset - script_offset - 4;
    write(u32value, NULL);
    stream->seekp(script_end_offset, stream->beg);
    write((uint32_t)script_crc32, NULL);

    int metadata_offset = stream->tellp();
    int metadata_end_offset;
    write("\x00\x00\x00\x00", 4, NULL);
    unsigned long metadata_crc32 = write_metadata();
    metadata_end_offset = stream->tellp();
    stream->seekp(metadata_offset, stream->beg);
    u32value = metadata_end_offset - metadata_offset - 4;
    write(u32value, NULL);
    stream->seekp(metadata_end_offset, stream->beg);
    write((uint32_t)metadata_crc32, NULL);

    for(auto p=previews->begin();p<previews->end();++p) {
        u32value = p->size();
        write(u32value, NULL);
        write(p->data(), u32value, NULL);
    }
    write("\x00\x00\x00\x00", 4, NULL);
}


FLUX::FCodeV1MemoryWriter::FCodeV1MemoryWriter(
        std::string *type, std::vector<std::pair<std::string, std::string> > *file_metadata,
        std::vector<std::string> *image_previews) : FCodeV1(type, file_metadata, image_previews) {
    stream = new std::stringstream();
    opened = true;
    begin();
}

FLUX::FCodeV1MemoryWriter::~FCodeV1MemoryWriter(void) {
    if(opened) {
        terminated();
    }
    delete stream;
}

std::string FLUX::FCodeV1MemoryWriter::get_buffer(void) {
    return ((std::stringstream*)stream)->str();
}

void FLUX::FCodeV1MemoryWriter::write(const char* buf, size_t size, unsigned long *crc32) {
    if(opened) {
        FLUX::FCodeV1Base::write(buf, size, crc32);
    }
}

void FLUX::FCodeV1MemoryWriter::terminated(void) {
    if(opened) {
        FLUX::FCodeV1::terminated();
        opened = false;
    }
}


FLUX::FCodeV1FileWriter::FCodeV1FileWriter(const char* filename,
        std::string *type, std::vector<std::pair<std::string, std::string> > *file_metadata,
        std::vector<std::string> *image_previews) : FCodeV1(type, file_metadata, image_previews) {
    stream = new std::ofstream(filename);
    if(stream->fail()) {
        throw std::runtime_error("OPEN FILE ERROR");
    }
    begin();
}


FLUX::FCodeV1FileWriter::~FCodeV1FileWriter(void) {
    delete stream;
}

void FLUX::FCodeV1FileWriter::write(const char* buf, size_t size, unsigned long *crc32) {
    if(((std::ofstream*)stream)->is_open()) {
        FLUX::FCodeV1Base::write(buf, size, crc32);
    }
}

void FLUX::FCodeV1FileWriter::terminated(void) {
    FLUX::FCodeV1::terminated();
    if(((std::ofstream*)stream)->is_open()) { ((std::ofstream*)stream)->close(); }
}
