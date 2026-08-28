using System;

namespace RotoWeave.Editor
{
    [Serializable]
    internal sealed class RotoWeaveCharacterManifest
    {
        public int formatVersion;
        public string packageShape;
        public string coordinateContract;
        public RotoWeaveGenerator generator;
        public RotoWeaveCharacterData character;
        public RotoWeaveRenderContract renderContract;
        public RotoWeaveLayeredTextureDefaults textureDefaults;
        public RotoWeaveAtlasLayers atlases;
        public RotoWeaveSprite[] sprites;
        public RotoWeaveAnimation[] animations;
    }

    [Serializable]
    internal sealed class RotoWeaveGenerator
    {
        public string name;
        public string version;
    }

    [Serializable]
    internal sealed class RotoWeaveCharacterData
    {
        public string id;
        public string name;
        public int revision;
        public int sourceRevision;
        public string defaultAnimationId;
        public float pixelsPerUnit;
        public float canonicalPixelsPerUnit;
        public float basePixelsPerUnit;
        public float outputScale;
        public RotoWeaveDesignSize designSize;
        public RotoWeaveShadowSettings shadow;
    }

    [Serializable]
    internal sealed class RotoWeaveDesignSize
    {
        public string profileId;
        public string displayName;
        public string sourceUnit;
        public float sourceWidth;
        public float sourceHeight;
        public int widthPixels;
        public int heightPixels;
        public float widthWorld;
        public float heightWorld;
        public float pixelsPerUnit;
    }

    [Serializable]
    internal sealed class RotoWeaveShadowSettings
    {
        public bool enabled;
        public RotoWeaveColor color;
        public float baseOpacity;
        public float lightAngleDegrees;
        public float rotationDegrees;
    }

    [Serializable]
    internal sealed class RotoWeaveColor
    {
        public float r;
        public float g;
        public float b;
    }

    [Serializable]
    internal sealed class RotoWeaveRenderContract
    {
        public string pipeline;
        public string target;
        public string colorSpace;
        public RotoWeaveLayerRenderContract @base;
        public RotoWeaveLayerRenderContract emission;
    }

    [Serializable]
    internal sealed class RotoWeaveLayerRenderContract
    {
        public string alphaMode;
        public string colorSpace;
        public RotoWeaveBlendContract blend;
    }

    [Serializable]
    internal sealed class RotoWeaveBlendContract
    {
        public string source;
        public string destination;
    }

    [Serializable]
    internal sealed class RotoWeaveLayeredTextureDefaults
    {
        public RotoWeaveTextureDefaults @base;
        public RotoWeaveTextureDefaults emission;
    }

    [Serializable]
    internal sealed class RotoWeaveTextureDefaults
    {
        public string format;
        public bool sRGB;
        public string wrapMode;
        public string filterMode;
        public bool mipmaps;
        public string compression;
    }

    [Serializable]
    internal sealed class RotoWeaveAtlasLayers
    {
        public RotoWeaveAtlas[] @base;
        public RotoWeaveAtlas[] emission;
    }

    [Serializable]
    internal sealed class RotoWeaveAtlas
    {
        public string id;
        public string file;
        public int width;
        public int height;
        public string sha256;
    }

    [Serializable]
    internal sealed class RotoWeaveAnimation
    {
        public string id;
        public string displayName;
        public float unityScale;
        public float outputScale;
        public float pixelsPerUnit;
        public bool loop;
        public float frameRate;
        public float durationSeconds;
        public RotoWeaveFrame[] frames;
    }

    [Serializable]
    internal sealed class RotoWeaveFrame
    {
        public string id;
        public string spriteId;
        public int index;
        public float durationSeconds;
        public RotoWeaveFrameTransform transform;
        public RotoWeaveShadowFrame shadow;
    }

    [Serializable]
    internal sealed class RotoWeaveSprite
    {
        public string id;
        public string atlasId;
        public RotoWeaveRect rect;
        public RotoWeavePivot pivot;
        public float outputScale;
        public string sourceSha256;
        public string emissionSha256;
    }

    [Serializable]
    internal sealed class RotoWeaveFrameTransform
    {
        public RotoWeaveVector2 position;
        public RotoWeaveVector2 scale;
        public float rotationDegrees;
        public string color;
        public float opacity;
        public RotoWeaveTransformShadow shadow;
    }

    [Serializable]
    internal sealed class RotoWeaveTransformShadow
    {
        public bool enabled;
        public string color;
        public float opacity;
        public RotoWeaveVector2 offset;
        public RotoWeaveVector2 scale;
    }

    [Serializable]
    internal sealed class RotoWeaveShadowFrame
    {
        public RotoWeaveVector2 positionPx;
        public float widthPx;
        public float depthPx;
        public float rotationDegrees;
        public float alpha;
        public float airborneRatio;
    }

    [Serializable]
    internal sealed class RotoWeaveVector2
    {
        public float x;
        public float y;
    }

    [Serializable]
    internal sealed class RotoWeaveRect
    {
        public int x;
        public int y;
        public int width;
        public int height;
    }

    [Serializable]
    internal sealed class RotoWeavePivot
    {
        public float x;
        public float y;
    }

    [Serializable]
    internal sealed class RotoWeaveChecksums
    {
        public string algorithm;
        public RotoWeaveChecksumFile[] files;
    }

    [Serializable]
    internal sealed class RotoWeaveChecksumFile
    {
        public string path;
        public string sha256;
    }
}
