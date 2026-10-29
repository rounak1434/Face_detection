# Test fixture provenance

All four images are official NASA portraits, **public domain in the United States**: their Wikimedia Commons pages state that the files are public domain because they were solely created by NASA, and quote NASA's policy that NASA material is not protected by copyright unless noted.

| File | Subject | Source (Wikimedia Commons) | Original | Changes | Retrieved |
|---|---|---|---|---|---|
| `nasa_s69-31741_480x600.jpg` | Neil Armstrong, Apollo 11 portrait | https://commons.wikimedia.org/wiki/File:Neil_Armstrong_pose.jpg | NASA photo ID **S69-31741**, 1 July 1969 | Downscaled to 480×600, JPEG quality 80 (about 50 KB) | 2026-10-23 |
| `nasa_armstrong_1964_480x600.jpg` | Neil Armstrong, astronaut portrait (a different photo of the same person) | https://commons.wikimedia.org/wiki/File:Astronaut_Neil_A._Armstrong_(1964).jpg (author NASA; via NASA Johnson Flickr, NASA Image and Video Library) | NASA, 1964 | Commons' 960×1200 thumbnail, downscaled to 480×600, JPEG quality 80, metadata dropped (37,838 bytes) | 2026-10-30 |
| `nasa_aldrin_1969_480x600.jpg` | Edwin E. "Buzz" Aldrin, Apollo 11 portrait | https://commons.wikimedia.org/wiki/File:Buzz_Aldrin.jpg (author NASA) | NASA, 16 April 1969 | As above (50,139 bytes) | 2026-10-30 |
| `nasa_collins_s69-31742_480x600.jpg` | Michael Collins, Apollo 11 portrait | https://commons.wikimedia.org/wiki/File:Michael_Collins_(S69-31742,_restoration).jpg (author NASA; restored scan by a Commons user) | NASA photo ID **S69-31742**, 16 April 1969 | As above (46,606 bytes) | 2026-10-30 |

**Use is limited to automated tests**: face detection, and from Phase 6 of ADR-0004 face-embedding and matching tests that enroll these images in temporary test databases only. They are public figures' official portraits, chosen because they are public domain; nothing in the product identifies these people, and no gallery of them is shipped. The two Armstrong photos are five years apart, so they test "same person, different photo"; Aldrin and Collins test "different people".

Do not add photos of private individuals to this repository. For your own real-camera checks, use `FACE_FIXTURE` / `IT_FACE_IMAGE` with a photo you are allowed to use, kept outside the repository.
